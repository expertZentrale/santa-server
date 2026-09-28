"""Read the identifiers Santa uses from a Mach-O file: SHA-256, and from the code signature
the Team ID, Signing ID, CDHash and leaf certificate SHA-256.

The signature is only parsed, not verified. Santa verifies it on the Mac.
"""
import hashlib
import logging
import struct
import warnings
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import pkcs7

logger = logging.getLogger(__name__)

FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
MH_MAGIC = 0xFEEDFACE
MH_MAGIC_64 = 0xFEEDFACF
MH_EXECUTE = 0x2
LC_CODE_SIGNATURE = 0x1D

CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_CODEDIRECTORY = 0xFADE0C02
CSMAGIC_BLOBWRAPPER = 0xFADE0B01
CSSLOT_CODEDIRECTORY = 0
CSSLOT_ALTERNATE_CODEDIRECTORIES = range(0x1000, 0x1005)
CSSLOT_SIGNATURESLOT = 0x10000
CS_ADHOC = 0x2

# hash type -> (hash function, rank). The kernel, and so Santa, uses the strongest code directory.
CD_HASH_TYPES = {1: ("sha1", 1), 3: ("sha256", 2), 2: ("sha256", 3), 4: ("sha384", 4)}

CPU_TYPES = {7: "i386", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64", 0x0200000C: "arm64_32"}
CHUNK_SIZE = 1024 * 1024


class NotMachO(Exception):
    pass


@dataclass
class SliceInfo:
    arch: str
    file_type: int
    team_id: str = ""
    signing_id: str = ""
    cdhash: str = ""
    cert_sha256: str = ""
    cert_cn: str = ""
    adhoc: bool = False
    platform: bool = False
    signed: bool = False


@dataclass
class MachOInfo:
    sha256: str
    size: int
    slices: list = field(default_factory=list)

    @property
    def is_executable(self):
        return any(s.file_type == MH_EXECUTE for s in self.slices)

    def _common(self, attr):
        values = {getattr(s, attr) for s in self.slices if getattr(s, attr)}
        return values.pop() if len(values) == 1 else ""

    @property
    def team_id(self):
        return self._common("team_id")

    @property
    def signing_id(self):
        return self._common("signing_id")

    @property
    def cert_sha256(self):
        return self._common("cert_sha256")

    @property
    def cert_cn(self):
        return self._common("cert_cn")

    @property
    def cdhashes(self):
        # one per architecture, Santa reports the one of the slice that ran
        return [s.cdhash for s in self.slices if s.cdhash]


def is_macho_header(header):
    if len(header) < 8:
        return False
    magic_be = struct.unpack(">I", header[:4])[0]
    magic_le = struct.unpack("<I", header[:4])[0]
    if magic_be in (FAT_MAGIC, FAT_MAGIC_64):
        # 0xCAFEBABE is also the Java class file magic, where the next word is the class version (>= 45)
        return 0 < struct.unpack(">I", header[4:8])[0] < 32
    return MH_MAGIC in (magic_be, magic_le) or MH_MAGIC_64 in (magic_be, magic_le)


def file_sha256(fileobj):
    fileobj.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := fileobj.read(CHUNK_SIZE):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def inspect(fileobj):
    """Return a MachOInfo for the seekable binary file object, raise NotMachO otherwise"""
    fileobj.seek(0)
    header = fileobj.read(8)
    if not is_macho_header(header):
        raise NotMachO("Not a Mach-O file")
    sha256, size = file_sha256(fileobj)
    info = MachOInfo(sha256=sha256, size=size)
    magic = struct.unpack(">I", header[:4])[0]
    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        nfat_arch = struct.unpack(">I", header[4:8])[0]
        entry_format, entry_size = (">iiQQI4x", 32) if magic == FAT_MAGIC_64 else (">iiIII", 20)
        fileobj.seek(8)
        entries = [struct.unpack(entry_format, fileobj.read(entry_size)) for _ in range(nfat_arch)]
        for _, _, offset, _, _ in entries:
            info.slices.append(_inspect_slice(fileobj, offset))
    else:
        info.slices.append(_inspect_slice(fileobj, 0))
    return info


def _inspect_slice(fileobj, offset):
    fileobj.seek(offset)
    raw_magic = fileobj.read(4)
    if struct.unpack("<I", raw_magic)[0] in (MH_MAGIC, MH_MAGIC_64):
        endian = "<"
    elif struct.unpack(">I", raw_magic)[0] in (MH_MAGIC, MH_MAGIC_64):
        endian = ">"
    else:
        raise NotMachO("Invalid Mach-O slice")
    magic = struct.unpack(endian + "I", raw_magic)[0]
    cputype, _, filetype, ncmds, _, _ = struct.unpack(endian + "iiIIII", fileobj.read(24))
    if magic == MH_MAGIC_64:
        fileobj.read(4)  # reserved
    slice_info = SliceInfo(arch=CPU_TYPES.get(cputype, hex(cputype)), file_type=filetype)
    for _ in range(ncmds):
        cmd_start = fileobj.tell()
        cmd, cmdsize = struct.unpack(endian + "II", fileobj.read(8))
        if cmd == LC_CODE_SIGNATURE:
            dataoff, datasize = struct.unpack(endian + "II", fileobj.read(8))
            fileobj.seek(offset + dataoff)
            try:
                _parse_signature(fileobj.read(datasize), slice_info)
            except (struct.error, ValueError) as e:
                logger.warning("Could not parse the code signature: %s", e)
            break
        if cmdsize < 8:
            raise NotMachO("Invalid load command")
        fileobj.seek(cmd_start + cmdsize)
    return slice_info


def _read_cstring(data, offset):
    end = data.index(b"\x00", offset)
    return data[offset:end].decode("utf-8", "replace")


def _parse_signature(blob, slice_info):
    magic, _, count = struct.unpack(">III", blob[:12])
    if magic != CSMAGIC_EMBEDDED_SIGNATURE:
        return
    slice_info.signed = True
    best_rank = 0
    for index in range(count):
        slot, blob_offset = struct.unpack(">II", blob[12 + index * 8:20 + index * 8])
        blob_magic, blob_length = struct.unpack(">II", blob[blob_offset:blob_offset + 8])
        sub_blob = blob[blob_offset:blob_offset + blob_length]
        if blob_magic == CSMAGIC_CODEDIRECTORY and (
            slot == CSSLOT_CODEDIRECTORY or slot in CSSLOT_ALTERNATE_CODEDIRECTORIES
        ):
            rank = _parse_code_directory(sub_blob, slice_info, best_rank)
            best_rank = max(best_rank, rank)
        elif slot == CSSLOT_SIGNATURESLOT and blob_magic == CSMAGIC_BLOBWRAPPER and blob_length > 8:
            _parse_cms(sub_blob[8:], slice_info)
    if slice_info.signing_id and not slice_info.adhoc:
        prefix = slice_info.team_id or ("platform" if slice_info.platform else "")
        slice_info.signing_id = f"{prefix}:{slice_info.signing_id}" if prefix else ""
    elif slice_info.adhoc:
        # Santa only supports signing IDs with a team ID or platform binaries
        slice_info.signing_id = ""


def _parse_code_directory(cd, slice_info, best_rank):
    version, flags, _, ident_offset = struct.unpack(">IIII", cd[8:24])
    hash_type = cd[37]
    hash_name, rank = CD_HASH_TYPES.get(hash_type, (None, 0))
    if rank <= best_rank or hash_name is None:
        return 0
    slice_info.cdhash = hashlib.new(hash_name, cd).hexdigest()[:40]
    slice_info.adhoc = bool(flags & CS_ADHOC)
    slice_info.platform = cd[38] != 0
    slice_info.signing_id = _read_cstring(cd, ident_offset)
    if version >= 0x20200:
        team_offset = struct.unpack(">I", cd[48:52])[0]
        if team_offset:
            slice_info.team_id = _read_cstring(cd, team_offset)
    return rank


def _parse_cms(data, slice_info):
    try:
        # Apple's signatures are BER encoded, cryptography falls back to BER with a deprecation warning.
        # If a future version refuses them, only the certificate identifiers are lost.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            certificates = pkcs7.load_der_pkcs7_certificates(data)
    except ValueError:
        return
    if not certificates:
        return
    issuers = {cert.issuer for cert in certificates}
    leaves = [cert for cert in certificates if cert.subject not in issuers] or certificates[:1]
    leaf = leaves[0]
    slice_info.cert_sha256 = leaf.fingerprint(hashes.SHA256()).hex()
    cn = leaf.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    slice_info.cert_cn = cn[0].value if cn else ""

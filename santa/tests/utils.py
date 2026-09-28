import hashlib
import struct

from santa.macho import CSMAGIC_CODEDIRECTORY, CSMAGIC_EMBEDDED_SIGNATURE, LC_CODE_SIGNATURE, MH_MAGIC_64

ARM64 = 0x0100000C
X86_64 = 0x01000007


def build_code_directory(identifier, team_id="", adhoc=False, platform=0):
    ident = identifier.encode() + b"\0"
    team = team_id.encode() + b"\0" if team_id else b""
    header_size = 52
    team_offset = header_size + len(ident) if team_id else 0
    length = header_size + len(ident) + len(team)
    cd = struct.pack(">IIIIIIIIIBBBBIII", CSMAGIC_CODEDIRECTORY, length, 0x20200, 0x2 if adhoc else 0, length,
                     header_size, 0, 0, 0, 32, 2, platform, 12, 0, 0, team_offset)
    return cd + ident + team


def build_signature(code_directory):
    return struct.pack(">IIIII", CSMAGIC_EMBEDDED_SIGNATURE, 20 + len(code_directory), 1, 0, 20) + code_directory


def build_macho(identifier="com.example.tool", team_id="ABCDE12345", adhoc=False, cputype=ARM64, filetype=2,
                signed=True):
    signature = build_signature(build_code_directory(identifier, team_id, adhoc)) if signed else b""
    ncmds = 1 if signed else 0
    header = struct.pack("<IiiIIIII", MH_MAGIC_64, cputype, 0, filetype, ncmds, 16 * ncmds, 0, 0)
    load_commands = struct.pack("<IIII", LC_CODE_SIGNATURE, 16, 4096, len(signature)) if signed else b""
    body = header + load_commands
    return body + b"\0" * (4096 - len(body)) + signature


def build_fat(*slices):
    offset = 4096
    entries = b""
    data = b""
    for index, (cputype, slice_data) in enumerate(slices):
        entries += struct.pack(">iiIII", cputype, 0, offset + len(data), len(slice_data), 12)
        data += slice_data
        padding = (-len(data)) % 4096
        data += b"\0" * padding
    header = struct.pack(">II", 0xCAFEBABE, len(slices)) + entries
    return header + b"\0" * (offset - len(header)) + data


def cdhash_of(identifier, team_id="", adhoc=False):
    return hashlib.sha256(build_code_directory(identifier, team_id, adhoc)).hexdigest()[:40]

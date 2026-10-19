"""The executables of a Homebrew bottle as Homebrew installs them, so their rules carry the hash the Macs see.

When Homebrew pours a bottle, it replaces the placeholders (@@HOMEBREW_CELLAR@@, …) in the load commands that name
libraries and run paths with the install locations, the way ruby-macho does (MIT license). On Apple silicon it then
signs the changed file again ad hoc (`codesign --sign - --force --preserve-metadata=…`). The bottles carry such an
ad-hoc signature already, and signing again keeps its layout: only the page hashes of the code directory change.

What can't be reproduced raises Unpredictable: the rules keep the hash of the bottle then.
"""
import hashlib
import struct

from . import macho

PLACEHOLDER = b"@@HOMEBREW_"

LC_SEGMENT = 0x1
LC_SEGMENT_64 = 0x19
LC_LOAD_DYLIB = 0xC
LC_ID_DYLIB = 0xD
LC_LOAD_WEAK_DYLIB = 0x80000018
LC_REEXPORT_DYLIB = 0x8000001F
LC_LAZY_LOAD_DYLIB = 0x20
LC_LOAD_UPWARD_DYLIB = 0x80000023
LC_RPATH = 0x8000001C
# the load commands Homebrew relocates, and the size of their fixed part (where ruby-macho puts the string)
STRING_COMMANDS = {LC_LOAD_DYLIB: 24, LC_ID_DYLIB: 24, LC_LOAD_WEAK_DYLIB: 24, LC_REEXPORT_DYLIB: 24,
                   LC_LAZY_LOAD_DYLIB: 24, LC_LOAD_UPWARD_DYLIB: 24, LC_RPATH: 12}

S_ZEROFILL = 0x1
S_THREAD_LOCAL_ZEROFILL = 0x12
CS_LINKER_SIGNED = 0x20000
# hash type of a code directory -> (hash function, its size in the code slots)
CD_HASHES = {1: ("sha1", 20), 2: ("sha256", 32), 3: ("sha256", 20), 4: ("sha384", 48)}


class Unpredictable(Exception):
    pass


def is_apple_silicon(bottle_tag):
    return bottle_tag.startswith("arm64_")


def homebrew_locations(bottle_tag):
    """The placeholders of a bottle and where Homebrew installs it by default on the Macs of its tag"""
    if is_apple_silicon(bottle_tag):
        prefix, repository = "/opt/homebrew", "/opt/homebrew"
    else:
        prefix, repository = "/usr/local", "/usr/local/Homebrew"
    return {b"@@HOMEBREW_PREFIX@@": prefix.encode(), b"@@HOMEBREW_CELLAR@@": f"{prefix}/Cellar".encode(),
            b"@@HOMEBREW_REPOSITORY@@": repository.encode(),
            b"@@HOMEBREW_LIBRARY@@": f"{repository}/Library".encode()}


def relocate(fileobj, bottle_tag):
    """Change the Mach-O file in fileobj (seekable, writable) as Homebrew does when it installs the bottle of the
    tag. Returns whether it changed. Raises Unpredictable when the result can't be known, before writing anything."""
    try:
        offsets = macho.slice_offsets(fileobj)
        locations = homebrew_locations(bottle_tag)
        changes = [write for offset in offsets for write in _relocated_slice(fileobj, offset, locations)]
        if not changes:
            return False
        # codesign signs every architecture of the file again, the unchanged ones come out the same
        directories = ([(offset, directory) for offset in offsets for directory in _code_directories(fileobj, offset)]
                       if is_apple_silicon(bottle_tag) else [])
    except struct.error as e:
        raise Unpredictable(f"invalid Mach-O ({e})") from e
    for position, data in changes:
        fileobj.seek(position)
        fileobj.write(data)
    for offset, directory in directories:
        _rehash(fileobj, offset, *directory)
    return True


def _slice_header(fileobj, offset):
    fileobj.seek(offset)
    raw = fileobj.read(28)
    if len(raw) < 28:
        raise Unpredictable("truncated Mach-O header")
    for endian in "<>":
        magic = struct.unpack(endian + "I", raw[:4])[0]
        if magic in (macho.MH_MAGIC, macho.MH_MAGIC_64):
            break
    else:
        raise Unpredictable("not a Mach-O slice")
    is_64 = magic == macho.MH_MAGIC_64
    ncmds, sizeofcmds = struct.unpack(endian + "II", raw[16:24])
    return endian, is_64, 32 if is_64 else 28, ncmds, sizeofcmds


def _load_commands(fileobj, offset):
    endian, is_64, header_size, ncmds, sizeofcmds = _slice_header(fileobj, offset)
    fileobj.seek(offset + header_size)
    raw = fileobj.read(sizeofcmds)
    commands = []
    position = 0
    for _ in range(ncmds):
        if position + 8 > len(raw):
            raise Unpredictable("invalid load commands")
        cmd, cmdsize = struct.unpack_from(endian + "II", raw, position)
        if cmdsize < 8 or position + cmdsize > len(raw):
            raise Unpredictable("invalid load command")
        commands.append((cmd, raw[position:position + cmdsize]))
        position += cmdsize
    return endian, is_64, header_size, commands


def _lowest_section_offset(cmd, data, endian):
    """ruby-macho's low_fileoff: the load commands may grow up to the first section with data in the file"""
    fixed, section_size, nsects_at, offset_at, flags_at = (
        (72, 80, 64, 48, 64) if cmd == LC_SEGMENT_64 else (56, 68, 48, 40, 56))
    nsects = struct.unpack_from(endian + "I", data, nsects_at)[0]
    lowest = None
    for index in range(nsects):
        start = fixed + index * section_size
        if start + section_size > len(data):
            raise Unpredictable("invalid segment")
        size = (struct.unpack_from(endian + "Q", data, start + 40)[0] if cmd == LC_SEGMENT_64
                else struct.unpack_from(endian + "I", data, start + 36)[0])
        section_offset = struct.unpack_from(endian + "I", data, start + offset_at)[0]
        section_type = struct.unpack_from(endian + "I", data, start + flags_at)[0] & 0xFF
        if size == 0 or section_type in (S_ZEROFILL, S_THREAD_LOCAL_ZEROFILL):
            continue
        lowest = section_offset if lowest is None else min(lowest, section_offset)
    return lowest


def _relocated_command(cmd, data, endian, alignment, locations):
    """The load command with the install locations in its string, as ruby-macho serializes it, or None"""
    fixed = STRING_COMMANDS[cmd]
    string_offset = struct.unpack_from(endian + "I", data, 8)[0]
    end = data.find(b"\0", string_offset)
    string = data[string_offset:end if end >= 0 else len(data)]
    if PLACEHOLDER not in string:
        return None
    if string_offset != fixed:
        raise Unpredictable(f"load command {cmd:#x} with its string at {string_offset}")
    for placeholder, location in locations.items():
        string = string.replace(placeholder, location)
    if PLACEHOLDER in string:
        raise Unpredictable(f"unknown placeholder in {string.decode(errors='replace')}")
    payload = string + b"\0"
    payload += b"\0" * (-(fixed + len(payload)) % alignment)
    # the fixed fields stay (timestamp and versions of a dylib), only the size and the string change
    return struct.pack(endian + "II", cmd, fixed + len(payload)) + data[8:fixed] + payload


def _relocated_slice(fileobj, offset, locations):
    """[(position, bytes)] to write for the relocated load commands of a slice, [] when nothing changes"""
    endian, is_64, header_size, commands = _load_commands(fileobj, offset)
    alignment = 8 if is_64 else 4
    old_size = sum(len(data) for _cmd, data in commands)
    lowest = None
    relocated = []
    changed = False
    for cmd, data in commands:
        if cmd in (LC_SEGMENT, LC_SEGMENT_64):
            section = _lowest_section_offset(cmd, data, endian)
            if section is not None:
                lowest = section if lowest is None else min(lowest, section)
        if cmd in STRING_COMMANDS:
            new = _relocated_command(cmd, data, endian, alignment, locations)
            if new is not None:
                data = new
                changed = True
        relocated.append(data)
    if not changed:
        return []
    raw = b"".join(relocated)
    if lowest is not None and header_size + len(raw) > lowest:
        raise Unpredictable("no room for the longer load commands")
    # sizeofcmds (ncmds stays) and the commands; shorter: the space left behind is zeroed, as ruby-macho does
    return [(offset + 20, struct.pack(endian + "I", len(raw))),
            (offset + header_size, raw + b"\0" * max(0, old_size - len(raw)))]


def _code_directories(fileobj, offset):
    """The code directories of the ad-hoc signature of a slice that codesign writes again with new page hashes:
    [(position of their code slots, hash function, hash size, page size, code limit)]"""
    endian, _is_64, _header_size, commands = _load_commands(fileobj, offset)
    signature = next((data for cmd, data in commands if cmd == macho.LC_CODE_SIGNATURE), None)
    if signature is None:
        raise Unpredictable("not signed: Homebrew signs it")
    dataoff, datasize = struct.unpack_from(endian + "II", signature, 8)
    fileobj.seek(offset + dataoff)
    blob = fileobj.read(datasize)
    if len(blob) < 12 or struct.unpack_from(">I", blob)[0] != macho.CSMAGIC_EMBEDDED_SIGNATURE:
        raise Unpredictable("invalid code signature")
    count = struct.unpack_from(">I", blob, 8)[0]
    directories = []
    for index in range(count):
        slot, blob_offset = struct.unpack_from(">II", blob, 12 + index * 8)
        if slot == macho.CSSLOT_CODEDIRECTORY or slot in macho.CSSLOT_ALTERNATE_CODEDIRECTORIES:
            directory = _code_directory(blob[blob_offset:])
            directories.append((offset + dataoff + blob_offset + directory[0], *directory[1:]))
    if not directories:
        raise Unpredictable("no code directory")
    return directories


def _code_directory(cd):
    magic, _length, version, flags, hash_offset, _ident, _special, code_slots, code_limit = struct.unpack_from(
        ">IIIIIIIII", cd)
    hash_size, hash_type, _platform, page_size = cd[36:40]
    if magic != macho.CSMAGIC_CODEDIRECTORY:
        raise Unpredictable("invalid code directory")
    if flags & CS_LINKER_SIGNED:
        raise Unpredictable("signed by the linker: codesign writes a new signature")
    if not flags & macho.CS_ADHOC:
        raise Unpredictable("not signed ad hoc")
    if version >= 0x20300 and len(cd) >= 64 and struct.unpack_from(">Q", cd, 56)[0]:
        raise Unpredictable("64-bit code limit")
    hash_name, expected_size = CD_HASHES.get(hash_type, (None, 0))
    if hash_name is None or hash_size != expected_size:
        raise Unpredictable(f"code directory hash type {hash_type}")
    page = 1 << page_size if page_size else max(code_limit, 1)
    if code_slots != -(-code_limit // page) or hash_offset + code_slots * hash_size > len(cd):
        raise Unpredictable("code slots don't cover the code")
    return hash_offset, hash_name, hash_size, page, code_limit


def _rehash(fileobj, slice_offset, slots_position, hash_name, hash_size, page, code_limit):
    hashes = []
    for start in range(0, code_limit, page):
        fileobj.seek(slice_offset + start)
        hashes.append(hashlib.new(hash_name, fileobj.read(min(page, code_limit - start))).digest()[:hash_size])
    fileobj.seek(slots_position)
    fileobj.write(b"".join(hashes))

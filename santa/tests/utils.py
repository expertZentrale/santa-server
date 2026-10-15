import hashlib
import lzma
import os
import struct
import subprocess
import zlib

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


def build_cpio(files, newc=False):
    """A cpio archive of {path: (data, mode)}, as the Payload of a package (odc, or newc)"""
    out = b""
    for index, (path, (data, mode)) in enumerate([*files.items(), ("TRAILER!!!", (b"", 0))]):
        name = path.encode() + b"\0"
        if newc:
            fields = [index + 1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0]
            header = b"070701" + b"".join(b"%08x" % field for field in fields)
            out += header + name + b"\0" * (-(len(header) + len(name)) % 4) + data + b"\0" * (-len(data) % 4)
        else:
            out += (b"070707" + b"%06o" % 0 + b"%06o" % (index + 1) + b"%06o" % mode + b"%06o" % 0 * 3
                    + b"%06o" % 0 + b"%011o" % 0 + b"%06o" % len(name) + b"%011o" % len(data) + name + data)
    return out


def build_pbzx(data, chunk_size=1024):
    """Apple's pbzx: xz chunks"""
    out = b"pbzx" + struct.pack(">Q", chunk_size)
    for start in range(0, len(data), chunk_size):
        chunk = lzma.compress(data[start:start + chunk_size], format=lzma.FORMAT_XZ)
        out += struct.pack(">QQ", chunk_size, len(chunk)) + chunk
    return out


def build_pkg(files, zlib_encoded=()):
    """A flat package (xar) of {path in the package: data}; the paths in zlib_encoded are stored zlib compressed"""
    heap = b""
    entries = {}
    for path, data in files.items():
        stored = zlib.compress(data) if path in zlib_encoded else data
        encoding = "application/x-gzip" if path in zlib_encoded else "application/octet-stream"
        entries[path] = (f"<data><offset>{len(heap)}</offset><length>{len(stored)}</length><size>{len(data)}</size>"
                         f'<encoding style="{encoding}"/></data>')
        heap += stored

    def xml(prefix):
        children = sorted({path[len(prefix):].split("/", 1)[0] for path in files if path.startswith(prefix)})
        out = ""
        for name in children:
            path = prefix + name
            if path in entries:
                out += f"<file><name>{name}</name><type>file</type>{entries[path]}</file>"
            else:
                out += f"<file><name>{name}</name><type>directory</type>{xml(path + '/')}</file>"
        return out

    toc = f'<?xml version="1.0" encoding="UTF-8"?><xar><toc>{xml("")}</toc></xar>'.encode()
    compressed = zlib.compress(toc)
    return struct.pack(">4sHHQQI", b"xar!", 28, 1, len(compressed), len(toc), 0) + compressed + heap


def build_dmg(data=b""):
    """Enough of a disk image to be recognized: the koly trailer (7-Zip is mocked in the tests)"""
    return data + b"koly" + b"\0" * 508


class FakeSevenZip:
    """subprocess.run of 7-Zip: lists and unpacks {path: (data, mode)}"""

    def __init__(self, files):
        self.files = files
        self.calls = []

    def listing(self):
        blocks = [f"Path = {path}\nFolder = -\nSize = {len(data)}\nMode = {mode}\nAlternate Stream = -\n"
                  for path, (data, mode) in self.files.items()]
        blocks.append("Path = Tool\nFolder = +\nSize = \nMode = drwxr-xr-x\n")
        blocks.append("Path = Tool/Applications\nFolder = -\nSize = 13\nMode = lrwxr-xr-x\n")
        return "\n".join(blocks)

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[1] == "l":
            return subprocess.CompletedProcess(args, 0, self.listing().encode(), b"")
        target = next(arg[2:] for arg in args if arg.startswith("-o"))
        list_file = next(arg[3:] for arg in args if arg.startswith("-i@"))
        with open(list_file, encoding="utf-8") as wanted:
            for path in wanted.read().splitlines():
                os.makedirs(os.path.join(target, os.path.dirname(path)), exist_ok=True)
                with open(os.path.join(target, path), "wb") as out:
                    out.write(self.files[path][0])
        return subprocess.CompletedProcess(args, 0, b"", b"")

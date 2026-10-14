"""The macOS installer formats for releases.find_binaries: flat packages (.pkg) and disk images (.dmg).

A flat package is a xar archive. Its Payload and Scripts files are cpio archives, compressed with gzip, bzip2 or
Apple's pbzx (xz chunks). Python reads all of that. A disk image (UDIF with HFS+ or APFS, zlib, bzip2, LZFSE or LZMA
blocks) is unpacked by 7-Zip (`7zz`), which the image installs.

The readers yield (path, size, executable, open): executable is the x bit of the file (None when unknown), and open()
returns the file to read. Members of a package are a stream: open() only works until the next member.
"""
import bz2
import gzip
import io
import lzma
import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from contextlib import contextmanager
from xml.etree import ElementTree

XAR_MAGIC = b"xar!"
XAR_HEADER = struct.Struct(">4sHHQQI")
MAX_TOC_BYTES = 16 * 1024 * 1024
# the cpio archives of a package, the rest (Bom, PackageInfo, Distribution, …) is metadata
PACKAGE_ARCHIVES = ("Payload", "Scripts")
PBZX_MAGIC = b"pbzx"
PBZX_MAX_CHUNK = 64 * 1024 * 1024
XZ_MAGIC = b"\xfd7zXZ\x00"
CPIO_ODC = b"070707"
CPIO_NEWC = (b"070701", b"070702")
CPIO_TRAILER = "TRAILER!!!"
S_IFMT, S_IFREG = 0o170000, 0o100000
SEVEN_ZIP = "7zz"


class ArchiveError(Exception):
    pass


def is_pkg(header):
    return header[:4] == XAR_MAGIC


def is_dmg(fileobj):
    """UDIF: the "koly" trailer is the last 512 bytes"""
    fileobj.seek(0, os.SEEK_END)
    if fileobj.tell() < 512:
        fileobj.seek(0)
        return False
    fileobj.seek(-512, os.SEEK_END)
    magic = fileobj.read(4)
    fileobj.seek(0)
    return magic == b"koly"


# Flat packages


class _Limited(io.RawIOBase):
    """A part of a file: the data of a xar entry, or of a cpio member"""

    def __init__(self, fileobj, size):
        self.fileobj = fileobj
        self.remaining = size

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.remaining <= 0:
            return 0
        data = self.fileobj.read(min(len(buffer), self.remaining))
        if not data:
            raise ArchiveError("the archive ends early")
        buffer[:len(data)] = data
        self.remaining -= len(data)
        return len(data)

    def skip(self):
        # also after the reader closed it
        while self.remaining > 0:
            data = self.fileobj.read(min(1024 * 1024, self.remaining))
            if not data:
                raise ArchiveError("the archive ends early")
            self.remaining -= len(data)


class _Counted(io.RawIOBase):
    """A decompressed stream that charges every byte to the unpack budget"""

    def __init__(self, stream, consume):
        self.stream = stream
        self.consume = consume

    def readable(self):
        return True

    def readinto(self, buffer):
        data = self.stream.read(len(buffer))
        self.consume(len(data))
        buffer[:len(data)] = data
        return len(data)


def _xar_entries(fileobj):
    """(path, offset, length, encoding) of the files of a xar archive; offset: in the file"""
    fileobj.seek(0)
    header = fileobj.read(XAR_HEADER.size)
    if len(header) < XAR_HEADER.size:
        raise ArchiveError("not a xar archive")
    magic, header_size, _version, toc_length, toc_size, _checksum = XAR_HEADER.unpack(header)
    if magic != XAR_MAGIC or toc_length > MAX_TOC_BYTES or toc_size > MAX_TOC_BYTES:
        raise ArchiveError("not a xar archive, or its table of contents is too big")
    fileobj.seek(header_size)
    try:
        decompressor = zlib.decompressobj()
        toc = decompressor.decompress(fileobj.read(toc_length), MAX_TOC_BYTES)
        root = ElementTree.fromstring(toc)
    except (zlib.error, ElementTree.ParseError) as e:
        raise ArchiveError(f"broken table of contents ({e})") from e
    heap = header_size + toc_length
    fileobj.seek(0, os.SEEK_END)
    end = fileobj.tell()
    entries = []

    def walk(node, prefix):
        for entry in node.findall("file"):
            path = prefix + (entry.findtext("name") or "")
            data = entry.find("data")
            if entry.findtext("type") == "file" and data is not None:
                try:
                    offset, length = int(data.findtext("offset")), int(data.findtext("length"))
                except (TypeError, ValueError):
                    raise ArchiveError(f"{path}: no offset or length") from None
                if offset < 0 or length < 0 or heap + offset + length > end:
                    raise ArchiveError(f"{path}: outside of the archive")
                encoding = data.find("encoding")
                entries.append((path, heap + offset, length,
                                encoding.get("style", "") if encoding is not None else ""))
            walk(entry, path + "/")

    toc_node = root.find("toc")
    if toc_node is None:
        raise ArchiveError("no table of contents")
    walk(toc_node, "")
    return entries


def _xar_data(fileobj, offset, length, encoding):
    """The data of a xar entry, decoded with its encoding (the files of a package are stored as they are)"""
    fileobj.seek(offset)
    raw = io.BufferedReader(_Limited(fileobj, length))
    if encoding in ("", "application/octet-stream"):
        return raw
    if encoding == "application/x-gzip":
        # xar calls zlib "gzip"
        return io.BufferedReader(_Zlib(raw))
    if encoding == "application/x-bzip2":
        return bz2.BZ2File(raw)
    if encoding in ("application/x-lzma", "application/x-xz"):
        return lzma.LZMAFile(raw)
    raise ArchiveError(f"unknown encoding {encoding}")


class _Zlib(io.RawIOBase):
    def __init__(self, raw):
        self.raw = raw
        self.decompressor = zlib.decompressobj()

    def readable(self):
        return True

    def readinto(self, buffer):
        try:
            while not self.decompressor.eof:
                tail = self.decompressor.unconsumed_tail
                chunk = tail or self.raw.read(64 * 1024)
                if not chunk:
                    raise ArchiveError("the zlib data ends early")
                if data := self.decompressor.decompress(chunk, len(buffer)):
                    buffer[:len(data)] = data
                    return len(data)
        except zlib.error as e:
            raise ArchiveError(f"broken zlib data ({e})") from e
        return 0


class _Pbzx(io.RawIOBase):
    """Apple's pbzx: a header, then chunks of (flags, length, xz stream or raw data)"""

    def __init__(self, raw):
        self.raw = raw
        self.pending = b""

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.pending:
            header = self.raw.read(16)
            if not header:
                return 0
            if len(header) < 16:
                raise ArchiveError("broken pbzx chunk")
            _flags, length = struct.unpack(">QQ", header)
            chunk = self.raw.read(length)
            if len(chunk) < length:
                raise ArchiveError("broken pbzx chunk")
            if chunk.startswith(XZ_MAGIC):
                decompressor = lzma.LZMADecompressor()
                try:
                    # Apple writes chunks of 16 MiB unpacked
                    chunk = decompressor.decompress(chunk, PBZX_MAX_CHUNK)
                except lzma.LZMAError as e:
                    raise ArchiveError(f"broken pbzx chunk ({e})") from e
                if not decompressor.eof:
                    raise ArchiveError("pbzx chunk too big")
            self.pending = chunk
        data, self.pending = self.pending[:len(buffer)], self.pending[len(buffer):]
        buffer[:len(data)] = data
        return len(data)


def _decompressed(stream):
    """A cpio archive of a package, compressed with gzip, bzip2 or pbzx, or not at all"""
    stream = io.BufferedReader(stream) if not isinstance(stream, io.BufferedReader) else stream
    magic = stream.peek(6)[:6]
    if magic.startswith(b"\x1f\x8b"):
        return gzip.GzipFile(fileobj=stream)
    if magic.startswith(b"BZh"):
        return bz2.BZ2File(stream)
    if magic.startswith(PBZX_MAGIC):
        stream.read(12)
        return io.BufferedReader(_Pbzx(stream))
    if magic.startswith(XZ_MAGIC):
        return lzma.LZMAFile(stream)
    return stream


def _read_exactly(stream, size):
    data = stream.read(size)
    if len(data) < size:
        raise ArchiveError("the cpio archive ends early")
    return data


def cpio_members(stream):
    """(path, mode, size, data) of a cpio archive (odc or newc); data must be read before the next member"""
    while True:
        magic = stream.read(6)
        if not magic:
            return
        try:
            if magic == CPIO_ODC:
                header = _read_exactly(stream, 70)
                mode = int(header[12:18], 8)
                name_size, size = int(header[53:59], 8), int(header[59:70], 8)
                name = _read_exactly(stream, name_size)
                name_padding = data_padding = 0
            elif magic in CPIO_NEWC:
                header = _read_exactly(stream, 104)
                fields = [int(header[i:i + 8], 16) for i in range(0, 104, 8)]
                mode, size, name_size = fields[1], fields[6], fields[11]
                name = _read_exactly(stream, name_size)
                name_padding = -(110 + name_size) % 4
                data_padding = -size % 4
            else:
                raise ArchiveError("not a cpio archive")
        except ValueError:
            raise ArchiveError("broken cpio header") from None
        _read_exactly(stream, name_padding)
        path = name.rstrip(b"\0").decode("utf-8", "replace").removeprefix("./")
        if path == CPIO_TRAILER:
            return
        data = _Limited(stream, size)
        yield path, mode, size, data
        data.skip()
        _read_exactly(stream, data_padding)


def pkg_members(fileobj, consume, max_members):
    """(path, size, executable, open) of the files in the Payload and Scripts of a flat package.

    consume(bytes) is charged with everything decompressed, also the files that are skipped (the files kept are
    charged again by the caller's spool: a little stricter than needed).
    """
    count = 0
    for path, offset, length, encoding in _xar_entries(fileobj):
        if path.rsplit("/", 1)[-1] not in PACKAGE_ARCHIVES:
            continue
        stream = io.BufferedReader(_Counted(_decompressed(_xar_data(fileobj, offset, length, encoding)), consume))
        try:
            for member_path, mode, size, data in cpio_members(stream):
                count += 1
                if count > max_members:
                    return
                if mode & S_IFMT != S_IFREG:
                    continue
                yield f"{path}/{member_path}", size, bool(mode & 0o111), lambda data=data: data
        except (OSError, EOFError, lzma.LZMAError, zlib.error) as e:
            raise ArchiveError(f"{path}: broken archive ({e})") from e


# Disk images


@contextmanager
def _on_disk(fileobj):
    """A path of the file for 7-Zip: an upload already on disk, else a copy"""
    if hasattr(fileobj, "temporary_file_path"):
        yield fileobj.temporary_file_path()
        return
    with tempfile.NamedTemporaryFile(suffix=".dmg") as copy:
        fileobj.seek(0)
        shutil.copyfileobj(fileobj, copy)
        copy.flush()
        yield copy.name


def _seven_zip(args, timeout):
    executable = shutil.which(SEVEN_ZIP)
    if not executable:
        raise ArchiveError(".dmg files need 7-Zip (7zz) on the server")
    try:
        # -p-: an encrypted image fails instead of asking for a password
        result = subprocess.run([executable, *args, "-p-", "-bd", "-sccUTF-8"], capture_output=True,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise ArchiveError(f"7-Zip took more than {timeout} seconds") from None
    # 1: warnings (e.g. data after the end of the image)
    if result.returncode > 1:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ArchiveError(f"7-Zip cannot read it ({message[-1] if message else result.returncode})")
    return result.stdout.decode("utf-8", "replace")


def _dmg_listing(output):
    """(path, size, mode) of the regular files of `7zz l -slt`"""
    files = []
    for block in output.split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        mode = fields.get("Mode", "")
        if (not fields.get("Path") or fields.get("Folder") == "+" or fields.get("Alternate Stream") == "+"
                or not mode.startswith("-")):
            continue
        try:
            size = int(fields.get("Size") or 0)
        except ValueError:
            continue
        files.append((fields["Path"], size, mode))
    return files


def dmg_members(fileobj, select, consume, max_members, timeout):
    """(path, size, executable, open) of the files of a disk image that select(path, size, executable) wants.

    7-Zip unpacks only those, into a temporary directory; consume(bytes) is charged with their sizes first.
    """
    with _on_disk(fileobj) as path, tempfile.TemporaryDirectory() as target:
        listing = _dmg_listing(_seven_zip(["l", "-slt", "-ba", path], timeout))[:max_members]
        wanted = [(member, size, "x" in mode) for member, size, mode in listing
                  if select(member, size, "x" in mode)]
        if not wanted:
            return
        for _member, size, _executable in wanted:
            consume(size)
        list_file = os.path.join(target, ".files")
        with open(list_file, "w", encoding="utf-8") as files:
            files.write("".join(f"{member}\n" for member, _size, _executable in wanted))
        out = os.path.join(target, "out")
        # -spd: the names are no wildcards
        _seven_zip(["x", path, f"-o{out}", "-y", "-spd", f"-i@{list_file}"], timeout)
        root = os.path.realpath(out)
        for member, size, executable in wanted:
            extracted = os.path.realpath(os.path.join(out, member))
            # never follow a link out of the directory
            if not extracted.startswith(root + os.sep) or not os.path.isfile(extracted):
                continue
            yield member, size, executable, lambda extracted=extracted: open(extracted, "rb")

import gzip
import io
import os
import subprocess
from unittest.mock import patch

from django.test import TestCase

from santa.releases import ReleaseError, find_binaries

from .test_releases import tar_gz, zip_file
from .utils import FakeSevenZip, build_cpio, build_dmg, build_macho, build_pbzx, build_pkg

EXECUTABLE = 0o100755
REGULAR = 0o100644
DIRECTORY = 0o040755


def paths(found):
    return [path for path, _info in found]


class PackageTestCase(TestCase):
    def test_component_package(self):
        tool = build_macho(identifier="com.example.tool")
        payload = build_cpio({
            ".": (b"", DIRECTORY),
            "./Tool.app/Contents/MacOS/Tool": (tool, EXECUTABLE),
            # the x bit decides what is worth reading
            "./Tool.app/Contents/Resources/copy": (tool, REGULAR),
            "./Tool.app/Contents/Frameworks/libfoo.dylib": (build_macho(filetype=6), EXECUTABLE),
        })
        data = build_pkg({"tool.pkg/Payload": gzip.compress(payload), "tool.pkg/PackageInfo": b"<pkg-info/>",
                          "Distribution": b"<installer-gui-script/>"}, zlib_encoded={"Distribution"})
        found = list(find_binaries(io.BytesIO(data), "Tool.pkg"))
        self.assertEqual(paths(found), ["tool.pkg/Payload/Tool.app/Contents/MacOS/Tool"])
        self.assertEqual(found[0][1].signing_id, "ABCDE12345:com.example.tool")

    def test_pbzx_payload_newc_and_scripts(self):
        tool = build_macho(identifier="tool", team_id="", adhoc=True)
        helper = build_macho(identifier="com.example.postinstall")
        data = build_pkg({
            "Payload": build_pbzx(build_cpio({"usr/local/bin/tool": (tool, EXECUTABLE)}, newc=True)),
            "Scripts": gzip.compress(build_cpio({"./postinstall-helper": (helper, EXECUTABLE)})),
        }, zlib_encoded={"Scripts"})
        found = list(find_binaries(io.BytesIO(data), "tool.pkg", ""))
        self.assertEqual(paths(found), ["Payload/usr/local/bin/tool", "Scripts/postinstall-helper"])

    def test_pattern(self):
        payload = build_cpio({"bin/tool": (build_macho(identifier="tool"), EXECUTABLE),
                              "bin/other": (build_macho(identifier="other"), EXECUTABLE)})
        data = build_pkg({"Payload": gzip.compress(payload)})
        self.assertEqual(paths(find_binaries(io.BytesIO(data), "tools.pkg", "*/bin/tool")), ["Payload/bin/tool"])

    def test_everything_decompressed_counts(self):
        # a payload of zeros that only unpacks big: skipped files count too
        payload = build_cpio({"big": (b"\0" * 200_000, REGULAR)})
        data = build_pkg({"Payload": gzip.compress(payload)})
        with self.settings(RELEASE_MAX_UNPACKED_BYTES=100_000):
            with self.assertRaisesMessage(ReleaseError, "unpacks to more than"):
                list(find_binaries(io.BytesIO(data), "bomb.pkg"))

    def test_broken_packages(self):
        data = build_pkg({"Payload": gzip.compress(build_cpio({"bin/tool": (build_macho(), EXECUTABLE)}))})
        for broken, message in [
            (data[:28] + b"garbage" + data[35:], "broken table of contents"),
            (data[:-10], "outside of the archive"),
            (build_pkg({"Payload": gzip.compress(b"070707garbage")}), "cpio"),
            (build_pkg({"Payload": b"not cpio" * 10}), "not a cpio archive"),
        ]:
            with self.subTest(message), self.assertRaisesMessage(ReleaseError, message):
                list(find_binaries(io.BytesIO(broken), "broken.pkg"))

    def test_package_inside_an_archive(self):
        payload = build_cpio({"bin/tool": (build_macho(identifier="tool"), EXECUTABLE)})
        data = zip_file({"Tool/Tool.pkg": build_pkg({"Payload": gzip.compress(payload)}),
                         "Tool/README": b"x" * 5000})
        self.assertEqual(paths(find_binaries(io.BytesIO(data), "tool.zip")), ["Tool/Tool.pkg/Payload/bin/tool"])

    def test_archives_are_read_one_level_deep(self):
        inner = tar_gz({"bin/tool": build_macho(identifier="tool")})
        data = tar_gz({"outer.zip": zip_file({"inner.tar.gz": inner, "pad": b"x" * 5000})})
        self.assertEqual(paths(find_binaries(io.BytesIO(data), "deep.tar.gz")), [])

    def test_unknown_format(self):
        with self.assertRaisesMessage(ReleaseError, "not a Mach-O binary, zip or tar archive, package or disk image"):
            list(find_binaries(io.BytesIO(b"x" * 5000), "Tool.bin"))


@patch("santa.archives.shutil.which", return_value="/usr/bin/7zz")
class DiskImageTestCase(TestCase):
    def find(self, files, pattern="", name="Tool.dmg"):
        seven_zip = FakeSevenZip(files)
        with patch("santa.archives.subprocess.run", side_effect=seven_zip):
            return list(find_binaries(io.BytesIO(build_dmg(b"image")), name, pattern)), seven_zip

    def test_app_and_package_in_a_disk_image(self, which):
        tool = build_macho(identifier="com.example.tool")
        payload = build_cpio({"usr/local/bin/helper": (build_macho(identifier="com.example.helper"), EXECUTABLE)})
        found, seven_zip = self.find({
            "Tool/Tool.app/Contents/MacOS/Tool": (tool, "-rwxr-xr-x"),
            "Tool/Tool.app/Contents/Resources/big.car": (b"x" * 10_000, "-rw-r--r--"),
            "Tool/Install Helper.pkg": (build_pkg({"Payload": gzip.compress(payload)}), "-rw-r--r--"),
        })
        self.assertEqual(paths(found), ["Tool/Tool.app/Contents/MacOS/Tool",
                                        "Tool/Install Helper.pkg/Payload/usr/local/bin/helper"])
        self.assertEqual(found[0][1].signing_id, "ABCDE12345:com.example.tool")
        # only the executables and the package are unpacked, without a password prompt
        extract = seven_zip.calls[1]
        self.assertIn("-p-", extract)
        self.assertIn("-spd", extract)

    def test_pattern(self, which):
        found, _ = self.find({"Tool/Tool.app/Contents/MacOS/Tool": (build_macho(), "-rwxr-xr-x"),
                              "Tool/Tool.app/Contents/MacOS/helper": (build_macho(), "-rwxr-xr-x")},
                             pattern="*/MacOS/Tool")
        self.assertEqual(paths(found), ["Tool/Tool.app/Contents/MacOS/Tool"])

    def test_a_too_big_executable_is_named_without_unpacking_it(self, which):
        tool = build_macho(identifier="com.example.tool")
        big = build_macho(identifier="com.example.big") + b"x" * 60_000
        seven_zip = FakeSevenZip({"Tool/Tool.app/Contents/MacOS/Tool": (tool, "-rwxr-xr-x"),
                                  "Tool/Tool.app/Contents/MacOS/big": (big, "-rwxr-xr-x"),
                                  "Tool/Tool.app/Contents/Resources/data.bin": (b"x" * 60_000, "-rw-r--r--")})
        skipped = []
        with self.settings(RELEASE_MAX_FILE_BYTES=50_000), \
                patch("santa.archives.subprocess.run", side_effect=seven_zip):
            found = list(find_binaries(io.BytesIO(build_dmg(b"image")), "Tool.dmg", skipped=skipped))
        self.assertEqual(paths(found), ["Tool/Tool.app/Contents/MacOS/Tool"])
        self.assertEqual(skipped, [f"Tool/Tool.app/Contents/MacOS/big: {len(big)} bytes, bigger than "
                                   "RELEASE_MAX_FILE_BYTES (50000), not checked"])

    def test_nothing_to_unpack(self, which):
        found, seven_zip = self.find({"Tool/README": (b"x" * 5000, "-rw-r--r--")})
        self.assertEqual((found, len(seven_zip.calls)), ([], 1))

    def test_budget(self, which):
        binary = build_macho()
        with self.settings(RELEASE_MAX_UNPACKED_BYTES=len(binary)):
            with self.assertRaisesMessage(ReleaseError, "unpacks to more than"):
                self.find({"a/tool": (binary, "-rwxr-xr-x"), "b/tool": (binary, "-rwxr-xr-x")})

    def test_without_7zip(self, which):
        which.return_value = None
        with self.assertRaisesMessage(ReleaseError, "Tool.dmg: .dmg files need 7-Zip (7zz) on the server"):
            list(find_binaries(io.BytesIO(build_dmg()), "Tool.dmg"))

    def test_7zip_fails_or_takes_too_long(self, which):
        failed = subprocess.CompletedProcess([], 2, b"", b"ERROR: Can not open the file as archive\n")
        with patch("santa.archives.subprocess.run", return_value=failed):
            with self.assertRaisesMessage(ReleaseError, "7-Zip cannot read it (ERROR: Can not open the file"):
                list(find_binaries(io.BytesIO(build_dmg()), "Tool.dmg"))
        with patch("santa.archives.subprocess.run", side_effect=subprocess.TimeoutExpired("7zz", 120)):
            with self.assertRaisesMessage(ReleaseError, "7-Zip took more than 120 seconds"):
                list(find_binaries(io.BytesIO(build_dmg()), "Tool.dmg"))

    def test_a_link_out_of_the_directory_is_not_followed(self, which):
        seven_zip = FakeSevenZip({"Tool/tool": (build_macho(), "-rwxr-xr-x")})

        def run(args, **kwargs):
            if args[1] == "x":
                target = next(arg[2:] for arg in args if arg.startswith("-o"))
                os.makedirs(target)
                os.symlink("/etc", os.path.join(target, "Tool"))
                return subprocess.CompletedProcess(args, 0, b"", b"")
            return seven_zip(args, **kwargs)

        with patch("santa.archives.subprocess.run", side_effect=run):
            self.assertEqual(list(find_binaries(io.BytesIO(build_dmg()), "Tool.dmg")), [])

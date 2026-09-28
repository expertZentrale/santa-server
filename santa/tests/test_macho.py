import hashlib
import io

from django.test import SimpleTestCase

from santa import macho

from .utils import ARM64, X86_64, build_fat, build_macho, cdhash_of


class MachOTestCase(SimpleTestCase):
    def test_signed_binary(self):
        data = build_macho()
        info = macho.inspect(io.BytesIO(data))
        self.assertEqual(info.sha256, hashlib.sha256(data).hexdigest())
        self.assertTrue(info.is_executable)
        self.assertEqual(info.team_id, "ABCDE12345")
        self.assertEqual(info.signing_id, "ABCDE12345:com.example.tool")
        self.assertEqual(info.cdhashes, [cdhash_of("com.example.tool", "ABCDE12345")])

    def test_adhoc_binary_has_no_signing_id(self):
        info = macho.inspect(io.BytesIO(build_macho(identifier="colima-arm64", team_id="", adhoc=True)))
        self.assertEqual(info.team_id, "")
        self.assertEqual(info.signing_id, "")
        self.assertEqual(len(info.cdhashes), 1)

    def test_unsigned_binary(self):
        info = macho.inspect(io.BytesIO(build_macho(signed=False)))
        self.assertTrue(info.is_executable)
        self.assertEqual(info.cdhashes, [])
        self.assertFalse(info.slices[0].signed)

    def test_universal_binary(self):
        data = build_fat((ARM64, build_macho(cputype=ARM64)), (X86_64, build_macho(cputype=X86_64)))
        info = macho.inspect(io.BytesIO(data))
        self.assertEqual([s.arch for s in info.slices], ["arm64", "x86_64"])
        self.assertEqual(info.signing_id, "ABCDE12345:com.example.tool")
        self.assertEqual(len(info.cdhashes), 2)

    def test_dylib_is_not_executable(self):
        info = macho.inspect(io.BytesIO(build_macho(filetype=6)))
        self.assertFalse(info.is_executable)

    def test_not_macho(self):
        with self.assertRaises(macho.NotMachO):
            macho.inspect(io.BytesIO(b"#!/bin/sh\necho hello\n"))
        # Java class file, same magic as a fat binary
        with self.assertRaises(macho.NotMachO):
            macho.inspect(io.BytesIO(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 100))

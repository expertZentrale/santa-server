import hashlib
import io
import struct
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from santa import bottles, macho
from santa.models import ReleaseSource, Rule
from santa.releases import find_binaries, sync_release_source

from .test_releases import FakeResponse, fake_session, tar_gz
from .utils import X86_64, build_bottle_macho, build_fat


def load_command_strings(data, offset=0):
    """{cmd: [string]} of the dylib and run path commands of a thin slice"""
    ncmds, sizeofcmds = struct.unpack_from("<II", data, offset + 16)
    position, strings = offset + 32, {}
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, position)
        if cmd in bottles.STRING_COMMANDS:
            string_offset = struct.unpack_from("<I", data, position + 8)[0]
            raw = data[position + string_offset:position + cmdsize]
            strings.setdefault(cmd, []).append(raw.split(b"\0")[0].decode())
        position += cmdsize
    assert position == offset + 32 + sizeofcmds
    return strings


def page_hashes_match(data, offset=0):
    """The code slots of the signature are the hashes of the pages, as the kernel checks them"""
    signature = data.rindex(struct.pack(">I", macho.CSMAGIC_CODEDIRECTORY))
    hash_offset, _ident, _special, code_slots, code_limit = struct.unpack_from(">IIIII", data, signature + 16)
    slots = data[signature + hash_offset:signature + hash_offset + 32 * code_slots]
    pages = b"".join(hashlib.sha256(data[offset + start:offset + min(start + 4096, code_limit)]).digest()
                     for start in range(0, code_limit, 4096))
    return slots == pages


def relocated(data, tag="arm64_sequoia"):
    fileobj = io.BytesIO(data)
    changed = bottles.relocate(fileobj, tag)
    return changed, fileobj.getvalue()


class RelocateTestCase(SimpleTestCase):
    def test_placeholders_are_replaced_and_the_pages_hashed_again(self):
        original = build_bottle_macho(rpaths=[b"@@HOMEBREW_PREFIX@@/lib", b"@loader_path/../lib"])
        changed, data = relocated(original)
        self.assertTrue(changed)
        self.assertEqual(len(data), len(original))
        self.assertEqual(load_command_strings(data), {
            bottles.LC_LOAD_DYLIB: ["/opt/homebrew/Cellar/tool/1.0/lib/libtool.dylib"],
            bottles.LC_RPATH: ["/opt/homebrew/lib", "@loader_path/../lib"]})
        self.assertTrue(page_hashes_match(data))
        self.assertNotEqual(macho.inspect(io.BytesIO(data)).cdhashes, macho.inspect(io.BytesIO(original)).cdhashes)

    def test_the_longer_load_command_moves_the_next_ones(self):
        # 24 + 47 + 1 is a multiple of 8: one more byte needs a longer command
        dylib = b"@@HOMEBREW_CELLAR@@/tool/1.0/lib/libtools.dylib"
        original = build_bottle_macho(dylib=dylib, rpaths=[b"@loader_path/../lib"])
        _changed, data = relocated(original)
        self.assertEqual(struct.unpack_from("<I", data, 20)[0], struct.unpack_from("<I", original, 20)[0] + 8)
        self.assertEqual(load_command_strings(data)[bottles.LC_RPATH], ["@loader_path/../lib"])
        self.assertEqual(macho.inspect(io.BytesIO(data)).slices[0].adhoc, True)
        self.assertTrue(page_hashes_match(data))

    def test_intel_gets_the_usr_local_paths_without_signing_again(self):
        original = build_bottle_macho(cputype=X86_64)
        _changed, data = relocated(original, "sonoma")
        self.assertEqual(load_command_strings(data)[bottles.LC_LOAD_DYLIB],
                         ["/usr/local/Cellar/tool/1.0/lib/libtool.dylib"])
        self.assertEqual(data[8192:], original[8192:])
        self.assertFalse(page_hashes_match(data))

    def test_every_slice_of_a_universal_binary(self):
        original = build_fat((0x0100000C, build_bottle_macho()), (X86_64, build_bottle_macho(cputype=X86_64)))
        _changed, data = relocated(original)
        for offset in macho.slice_offsets(io.BytesIO(data)):
            self.assertEqual(load_command_strings(data, offset)[bottles.LC_LOAD_DYLIB],
                             ["/opt/homebrew/Cellar/tool/1.0/lib/libtool.dylib"])

    def test_without_placeholders_nothing_changes(self):
        original = build_bottle_macho(dylib=b"/usr/lib/libSystem.B.dylib")
        self.assertEqual(relocated(original), (False, original))

    def test_what_cant_be_predicted_is_left_as_it_is(self):
        # without its code signature command (the last one): Homebrew signs it, with a signature of its own
        unsigned = bytearray(build_bottle_macho())
        ncmds, sizeofcmds = struct.unpack_from("<II", unsigned, 16)
        struct.pack_into("<II", unsigned, 16, ncmds - 1, sizeofcmds - 16)
        cases = {
            "signed by the linker": build_bottle_macho(cd_flags=0x20002),
            "not signed ad hoc": build_bottle_macho(cd_flags=0),
            "not signed: Homebrew signs it": bytes(unsigned),
            "no room": build_bottle_macho(dylib=b"@@HOMEBREW_CELLAR@@/tool/1.0/lib/libtools.dylib", text_offset=0x110),
            "unknown placeholder": build_bottle_macho(dylib=b"@@HOMEBREW_JAVA@@/lib/libjvm.dylib"),
        }
        for reason, original in cases.items():
            with self.subTest(reason):
                fileobj = io.BytesIO(original)
                with self.assertRaisesMessage(bottles.Unpredictable, reason):
                    bottles.relocate(fileobj, "arm64_sequoia")
                self.assertEqual(fileobj.getvalue(), original)


class BottleBinariesTestCase(SimpleTestCase):
    def test_a_bottle_gives_the_installed_executable(self):
        executable = build_bottle_macho()
        _changed, installed = relocated(executable)
        linker_signed = build_bottle_macho(cd_flags=0x20002)
        bottle = tar_gz({"tool/1.0/bin/tool": executable, "tool/1.0/bin/other": linker_signed,
                         "tool/1.0/share/test.tar": tar_gz({"bin/x": executable})[:100]})
        found = dict(find_binaries(io.BytesIO(bottle), "tool-1.0.arm64_sequoia.bottle.tar.gz", "", "arm64_sequoia"))
        self.assertEqual(found["tool/1.0/bin/tool"].sha256, hashlib.sha256(installed).hexdigest())
        self.assertEqual(found["tool/1.0/bin/tool"].warning, "")
        # the hash of the bottle, with the reason
        self.assertEqual(found["tool/1.0/bin/other"].sha256, hashlib.sha256(linker_signed).hexdigest())
        self.assertIn("signed by the linker", found["tool/1.0/bin/other"].warning)
        # an archive in a bottle is data, not unpacked (a broken one doesn't fail the bottle)
        self.assertEqual(set(found), {"tool/1.0/bin/tool", "tool/1.0/bin/other"})
        # not a bottle: as it is
        [(_path, info)] = find_binaries(io.BytesIO(executable), "tool")
        self.assertEqual(info.sha256, hashlib.sha256(executable).hexdigest())


class HomebrewFormulaTestCase(TestCase):
    def setUp(self):
        self.source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                                   identifier="tool", asset_pattern="^arm64_", is_global=True)
        self.executable = build_bottle_macho()
        bottle = tar_gz({"tool/1.0/bin/tool": self.executable})
        formula = {"versions": {"stable": "1.0"}, "revision": 0, "bottle": {"stable": {"files": {
            "arm64_sequoia": {"url": "https://ghcr.example/arm64_sequoia",
                              "sha256": hashlib.sha256(bottle).hexdigest()}}}}}
        self.routes = {"https://formulae.brew.sh/api/formula/tool.json": FakeResponse(formula),
                       "https://ghcr.example/arm64_sequoia": FakeResponse(content=bottle)}
        _changed, installed = relocated(self.executable, "arm64_sequoia")
        self.installed = hashlib.sha256(installed).hexdigest()

    def test_the_rules_have_the_hash_of_the_installed_executable(self):
        source = self.source
        linker_signed = build_bottle_macho(cd_flags=0x20002)
        bottle = tar_gz({"tool/1.0/bin/tool": self.executable, "tool/1.0/bin/helper": linker_signed})
        formula = self.routes["https://formulae.brew.sh/api/formula/tool.json"].json()
        formula["bottle"]["stable"]["files"]["arm64_sequoia"]["sha256"] = hashlib.sha256(bottle).hexdigest()
        routes = {**self.routes, "https://ghcr.example/arm64_sequoia": FakeResponse(content=bottle)}
        installed = self.installed
        [version] = sync_release_source(source, fake_session(routes))
        self.assertEqual(set(Rule.objects.filter(release_source=source).values_list("identifier", flat=True)),
                         {installed, hashlib.sha256(linker_signed).hexdigest()})
        self.assertIn("tool/1.0/bin/helper: Homebrew changes it on install", version.notes)

    def test_versions_recorded_before_are_recorded_again(self):
        # recorded with the hash of the bottle, before the change
        with patch("santa.releases.bottles.relocate", return_value=False):
            sync_release_source(self.source, fake_session(self.routes))
        bottle_hash = hashlib.sha256(self.executable).hexdigest()
        self.assertEqual(list(Rule.objects.values_list("identifier", flat=True)), [bottle_hash])
        output = io.StringIO()
        with patch("santa.management.commands.sync_release_sources.http_session",
                   return_value=fake_session(self.routes)):
            call_command("sync_release_sources", "--rerecord-homebrew", stdout=output)
        self.assertIn("tool: 1 versions and 1 rules deleted", output.getvalue())
        self.assertEqual(list(Rule.objects.values_list("identifier", flat=True)), [self.installed])
        self.assertEqual(self.source.versions.count(), 1)

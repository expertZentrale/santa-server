import hashlib
import io
import struct
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from santa import bottles, macho
from santa.console.views_sources import latest_versions
from santa.models import ReleaseSource, ReleaseVersion, Rule, RuleType
from santa.releases import (
    ReleaseError,
    _ghcr_base,
    find_binaries,
    latest_homebrew_formula,
    sync_release_source,
)

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
        # recorded with the hash of the bottle, before the change: the current version, approved by hand, and an
        # older one the registry no longer has
        with patch("santa.releases.bottles.relocate", return_value=False):
            [current] = sync_release_source(self.source, fake_session(self.routes))
        bottle_hash = hashlib.sha256(self.executable).hexdigest()
        current.rules.update(is_enabled=False)
        older = ReleaseVersion.objects.create(source=self.source, identifier="tool", version="0.9", notes="old",
                                              binary_count=1)
        Rule.objects.create(rule_type=RuleType.BINARY, identifier="9" * 64, release_source=self.source,
                            release_version=older)

        def formula_at(session, source, name, version=None, before=None):
            if version != "1.0":
                raise ReleaseError(f"{name}: no version {version} in the Homebrew registry")
            return latest_homebrew_formula(session, source, name)
        output, errors = io.StringIO(), io.StringIO()
        with patch("santa.management.commands.sync_release_sources.http_session",
                   return_value=fake_session(self.routes)), \
                patch("santa.releases.homebrew_formula_at", side_effect=formula_at):
            call_command("sync_release_sources", "--rerecord-homebrew", stdout=output, stderr=errors)
        self.assertIn("tool: 1 versions recorded again", output.getvalue())
        self.assertIn("0.9: kept as it is (tool: no version 0.9 in the Homebrew registry)", errors.getvalue())
        # the new hash, still waiting for approval like the rule it replaces
        rule = current.rules.get()
        self.assertEqual((rule.identifier, rule.is_enabled), (self.installed, False))
        self.assertNotIn(bottle_hash, current.rules.values_list("identifier", flat=True))
        self.assertIn(self.installed, ReleaseVersion.objects.get(pk=current.pk).notes)
        # the older version keeps its rule
        self.assertEqual(list(older.rules.values_list("identifier", flat=True)), ["9" * 64])


class VersionDatesTestCase(TestCase):
    def test_the_current_version_gets_the_day_of_its_bottles(self):
        source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.HOMEBREW_FORMULA, identifier="tool",
                                              asset_pattern="^arm64_", is_global=True)
        bottle = tar_gz({"tool/1.0/bin/tool": build_bottle_macho()})
        formula = {"versions": {"stable": "1.0"}, "revision": 0, "bottle": {"stable": {"files": {
            "arm64_sequoia": {"url": "https://ghcr.example/arm64_sequoia",
                              "sha256": hashlib.sha256(bottle).hexdigest()}}}}}
        routes = {"https://formulae.brew.sh/api/formula/tool.json": FakeResponse(formula),
                  "https://ghcr.example/arm64_sequoia": FakeResponse(content=bottle)}
        # recorded before the day was asked: it gets it at the next check
        ReleaseVersion.objects.create(source=source, identifier="tool", version="1.0")
        routes[f"{_ghcr_base('tool')}/manifests/1.0"] = FakeResponse(
            {"annotations": {"org.opencontainers.image.created": "2026-10-01T10:00:00Z"}})
        sync_release_source(source, fake_session(routes))
        self.assertEqual(source.versions.get().published_at.isoformat(), "2026-10-01T10:00:00+00:00")
        # asked once: not again at the next check
        session = fake_session(routes)
        sync_release_source(source, session)
        self.assertNotIn(f"{_ghcr_base('tool')}/manifests/1.0", [call.args[0] for call in session.get.call_args_list])

    def test_the_latest_version_is_the_newest_release_not_the_last_recorded(self):
        source = ReleaseSource.objects.create(name="go", kind=ReleaseSource.Kind.HOMEBREW_FORMULA, identifier="go")
        current = ReleaseVersion.objects.create(source=source, identifier="go", version="1.27.2",
                                                published_at=timezone.now() - timedelta(days=3))
        # the older versions the package rule allows, recorded after it
        ReleaseVersion.objects.create(source=source, identifier="go", version="1.26.9",
                                      published_at=timezone.now() - timedelta(days=60))
        self.assertEqual(latest_versions(source), [("go", current)])

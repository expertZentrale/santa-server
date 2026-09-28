import base64
import hashlib

from django.core.exceptions import ValidationError

from santa.models import Policy, ReleaseSource, Rule, RuleType
from santa.releases import VSCODE_GALLERY_URL, ReleaseError, sync_release_source

from .test_releases import FakeResponse, ReleaseSourceBase, fake_session, tar_gz, zip_file
from .utils import build_macho


def formula_routes(name, version, binary):
    bottle = tar_gz({f"{name}/{version}/bin/{name}": binary})
    return {
        f"https://formulae.brew.sh/api/formula/{name}.json": FakeResponse({
            "versions": {"stable": version}, "revision": 0, "bottle": {"stable": {"files": {
                "arm64_sequoia": {"url": f"https://ghcr.example/{name}/{version}",
                                  "sha256": hashlib.sha256(bottle).hexdigest()},
            }}},
        }),
        f"https://ghcr.example/{name}/{version}": FakeResponse(content=bottle),
    }


def unsigned(name):
    return build_macho(identifier=name, team_id="", adhoc=True)


class SeveralIdentifiersTestCase(ReleaseSourceBase):
    def formula_source(self, **kwargs):
        return ReleaseSource.objects.create(name="dev tools", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                            identifier="colima\nlima", is_global=True, **kwargs)

    def test_every_identifier_has_its_own_versions(self):
        source = self.formula_source(keep_versions=1)
        routes = {**formula_routes("colima", "0.9.0", unsigned("colima")),
                  **formula_routes("lima", "1.2.0", unsigned("lima"))}
        new_versions = sync_release_source(source, fake_session(routes))
        self.assertEqual({(v.identifier, v.version) for v in new_versions}, {("colima", "0.9.0"), ("lima", "1.2.0")})
        self.assertEqual(str(new_versions[0]), "dev tools colima 0.9.0")
        self.assertEqual(Rule.objects.filter(release_source=source).count(), 2)

        # a new colima release replaces only the colima version
        routes.update(formula_routes("colima", "0.9.1", unsigned("colima-new")))
        routes["https://formulae.brew.sh/api/formula/colima.json"] = formula_routes(
            "colima", "0.9.1", unsigned("colima-new"))["https://formulae.brew.sh/api/formula/colima.json"]
        [new_version] = sync_release_source(source, fake_session(routes))
        self.assertEqual((new_version.identifier, new_version.version), ("colima", "0.9.1"))
        self.assertEqual(set(source.versions.values_list("identifier", "version")),
                         {("colima", "0.9.1"), ("lima", "1.2.0")})

    def test_a_failing_identifier_does_not_stop_the_others(self):
        source = self.formula_source()
        routes = formula_routes("lima", "1.2.0", unsigned("lima"))
        routes["https://formulae.brew.sh/api/formula/colima.json"] = FakeResponse({"versions": {}})
        with self.assertRaises(ReleaseError) as cm:
            sync_release_source(source, fake_session(routes))
        self.assertEqual([v.identifier for v in cm.exception.new_versions], ["lima"])
        self.assertEqual(list(source.versions.values_list("identifier", flat=True)), ["lima"])
        source.refresh_from_db()
        self.assertTrue(source.last_error.startswith("colima: "))

    def test_removed_identifier_loses_its_rules(self):
        source = self.formula_source()
        routes = {**formula_routes("colima", "0.9.0", unsigned("colima")),
                  **formula_routes("lima", "1.2.0", unsigned("lima"))}
        sync_release_source(source, fake_session(routes))
        source.identifier = "lima"
        source.save()
        self.assertEqual(sync_release_source(source, fake_session(routes)), [])
        self.assertEqual(list(source.versions.values_list("identifier", flat=True)), ["lima"])
        self.assertEqual(Rule.objects.filter(release_source=source).count(), 1)

    def test_clean(self):
        source = ReleaseSource(name="tools", kind=ReleaseSource.Kind.GITHUB_RELEASE,
                               identifier=" abiosoft/colima \n\nlima\nabiosoft/colima")
        with self.assertRaises(ValidationError) as cm:
            source.full_clean()
        self.assertEqual(cm.exception.message_dict["identifier"], ["lima: use the owner/repo format."])
        source.identifier = "abiosoft/colima\n lima-vm/lima\nabiosoft/colima\n"
        source.full_clean()
        self.assertEqual(source.identifier, "abiosoft/colima\nlima-vm/lima")
        source.identifier = "\n \n"
        with self.assertRaises(ValidationError):
            source.full_clean()


class RuleOptionsTestCase(ReleaseSourceBase):
    def test_preferred_rule_type_with_fallback(self):
        source = ReleaseSource.objects.create(name="tools", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="signed\nunsigned", rule_type=RuleType.SIGNINGID)
        routes = {**formula_routes("signed", "1.0", build_macho(identifier="com.example.signed")),
                  **formula_routes("unsigned", "1.0", unsigned("unsigned"))}
        sync_release_source(source, fake_session(routes))
        self.assertEqual(
            set(Rule.objects.filter(release_source=source).values_list("rule_type", "identifier")),
            {(RuleType.SIGNINGID, "ABCDE12345:com.example.signed"),
             (RuleType.BINARY, hashlib.sha256(unsigned("unsigned")).hexdigest())},
        )

    def test_policy_and_block_message_are_copied(self):
        source = ReleaseSource.objects.create(name="blocked", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", policy=Policy.CEL, cel_expr="false",
                                              custom_msg="Not allowed")
        sync_release_source(source, fake_session(formula_routes("colima", "0.9.0", unsigned("colima"))))
        rule = Rule.objects.get(release_source=source)
        self.assertEqual((rule.policy, rule.cel_expr, rule.custom_msg), (Policy.CEL, "false", "Not allowed"))
        self.assertEqual(rule.to_santa()["cel_expr"], "false")

    def test_version_pattern(self):
        source = ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA,
                                              identifier="colima", version_pattern=r"^0\.8\.")
        self.assertEqual(sync_release_source(source, fake_session(
            formula_routes("colima", "0.9.0", unsigned("colima")))), [])
        self.assertFalse(Rule.objects.exists())
        source.refresh_from_db()
        self.assertEqual(source.last_error, "")

    def test_invalid_patterns(self):
        source = ReleaseSource(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA, identifier="colima",
                               version_pattern="(")
        with self.assertRaises(ValidationError) as cm:
            source.full_clean()
        self.assertIn("version_pattern", cm.exception.message_dict)


class NpmTestCase(ReleaseSourceBase):
    def npm_routes(self, tarball, integrity_of=None):
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(integrity_of or tarball).digest()).decode()
        return {
            "https://registry.npmjs.org/@esbuild%2Fdarwin-arm64/latest": FakeResponse({
                "version": "0.28.2",
                "dist": {"tarball": "https://registry.example/darwin-arm64-0.28.2.tgz", "integrity": integrity},
            }),
            "https://registry.example/darwin-arm64-0.28.2.tgz": FakeResponse(content=tarball),
        }

    def test_npm_package(self):
        source = ReleaseSource.objects.create(name="esbuild", kind=ReleaseSource.Kind.NPM_PACKAGE,
                                              identifier="@esbuild/darwin-arm64", is_global=True)
        binary = unsigned("esbuild")
        tarball = tar_gz({"package/bin/esbuild": binary, "package/package.json": b"{}" * 3000})
        [version] = sync_release_source(source, fake_session(self.npm_routes(tarball)))
        self.assertEqual(version.version, "0.28.2")
        self.assertEqual(Rule.objects.get(release_source=source).identifier, hashlib.sha256(binary).hexdigest())

    def test_npm_integrity_is_checked(self):
        source = ReleaseSource.objects.create(name="esbuild", kind=ReleaseSource.Kind.NPM_PACKAGE,
                                              identifier="@esbuild/darwin-arm64")
        tarball = tar_gz({"package/bin/esbuild": unsigned("esbuild")})
        with self.assertRaises(ReleaseError):
            sync_release_source(source, fake_session(self.npm_routes(tarball + b"x", integrity_of=tarball)))
        source.refresh_from_db()
        self.assertIn("SHA-512 mismatch", source.last_error)


class VSCodeTestCase(ReleaseSourceBase):
    def gallery(self, versions):
        return {"results": [{"extensions": [{"versions": [
            {"version": version, "targetPlatform": platform, "lastUpdated": "2026-09-28T01:00:00Z",
             "properties": [{"key": "Microsoft.VisualStudio.Code.PreRelease", "value": "true"}] if pre else [],
             "files": [{"assetType": "Microsoft.VisualStudio.Services.VSIXPackage",
                        "source": f"https://vsassets.example/{version}/{platform}"}]}
            for version, platform, pre in versions
        ]}]}]}

    def test_vscode_extension(self):
        source = ReleaseSource.objects.create(name="rust-analyzer", kind=ReleaseSource.Kind.VSCODE_EXTENSION,
                                              identifier="rust-lang.rust-analyzer", is_global=True)
        arm = unsigned("rust-analyzer-arm64")
        intel = unsigned("rust-analyzer-x64")
        gallery = self.gallery([("0.4.3064", "darwin-arm64", True), ("0.3.3065", "linux-x64", False),
                                ("0.3.3065", "darwin-arm64", False), ("0.3.3065", "darwin-x64", False),
                                ("0.3.3065", "win32-x64", False)])
        session = fake_session({
            "https://vsassets.example/0.3.3065/darwin-arm64": FakeResponse(
                content=zip_file({"extension/server/rust-analyzer": arm})),
            "https://vsassets.example/0.3.3065/darwin-x64": FakeResponse(
                content=zip_file({"extension/server/rust-analyzer": intel})),
        })
        session.post.return_value = FakeResponse(gallery)
        [version] = sync_release_source(source, session)
        self.assertEqual(version.version, "0.3.3065")
        self.assertEqual(set(Rule.objects.filter(release_source=source).values_list("identifier", flat=True)),
                         {hashlib.sha256(arm).hexdigest(), hashlib.sha256(intel).hexdigest()})
        self.assertEqual(session.post.call_args.args[0], VSCODE_GALLERY_URL)

    def test_extension_without_executables_is_not_an_error(self):
        # e.g. ms-python.debugpy: only Python files, nothing for Santa
        source = ReleaseSource.objects.create(name="debugpy", kind=ReleaseSource.Kind.VSCODE_EXTENSION,
                                              identifier="ms-python.debugpy")
        session = fake_session({"https://vsassets.example/2026.6.0/None": FakeResponse(
            content=zip_file({"extension/bundled/libs/debugpy/__init__.py": b"#" * 5000}))})
        session.post.return_value = FakeResponse(self.gallery([("2026.6.0", None, False)]))
        [version] = sync_release_source(source, session)
        self.assertEqual((version.version, version.binary_count), ("2026.6.0", 0))
        self.assertIn("nothing to allow", version.notes)
        source.refresh_from_db()
        self.assertEqual(source.last_error, "")
        self.assertFalse(Rule.objects.exists())

    def test_github_release_without_executables_stays_an_error(self):
        source = self.github_source()
        routes = self.github_routes("v1.0.0", {"colima-Darwin-arm64": b"not a binary" * 1000})
        with self.assertRaises(ReleaseError):
            sync_release_source(source, fake_session(routes))

    def test_extension_without_mac_version(self):
        source = ReleaseSource.objects.create(name="tool", kind=ReleaseSource.Kind.VSCODE_EXTENSION,
                                              identifier="example.tool")
        session = fake_session({})
        session.post.return_value = FakeResponse(self.gallery([("1.0.0", "win32-x64", False)]))
        with self.assertRaises(ReleaseError):
            sync_release_source(source, session)


class JetBrainsTestCase(ReleaseSourceBase):
    def test_jetbrains_plugin_for_phpstorm(self):
        source = ReleaseSource.objects.create(name="watcher", kind=ReleaseSource.Kind.JETBRAINS_PLUGIN,
                                              identifier="com.example.watcher", asset_pattern="^PHPSTORM$",
                                              is_global=True)
        binary = unsigned("watcher-helper")
        routes = {
            "https://plugins.jetbrains.com/api/plugins/intellij/com.example.watcher": FakeResponse({"id": 7177}),
            "https://plugins.jetbrains.com/api/plugins/7177/updates?size=20&channel=": FakeResponse([
                {"version": "2.0", "file": "7177/2/watcher-2.0.zip", "cdate": "1790266356000",
                 "compatibleVersions": {"IDEA": "2026.3"}},
                {"version": "1.9", "file": "7177/1/watcher-1.9.zip", "cdate": "1789644848000",
                 "compatibleVersions": {"IDEA": "2026.2", "PHPSTORM": "2026.2"}},
            ]),
            "https://downloads.marketplace.jetbrains.com/files/7177/1/watcher-1.9.zip": FakeResponse(
                content=zip_file({"watcher/bin/helper": binary})),
        }
        [version] = sync_release_source(source, fake_session(routes))
        self.assertEqual(version.version, "1.9")
        self.assertEqual(version.published_at.year, 2026)
        self.assertEqual(Rule.objects.get(release_source=source).identifier, hashlib.sha256(binary).hexdigest())

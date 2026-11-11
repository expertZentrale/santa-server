from html.parser import HTMLParser

from django.contrib.auth.models import User
from django.urls import reverse

from santa.models import AccessRequest, FileAccessRule, Group, IgnoredApp, ReleaseSource, Rule, RuleType

from .test_console import SHA_A, ConsoleBase
from .test_list_rows import NAVIGATE


class Tables(HTMLParser):
    """The tables of the lists (data-table): their merges, the keys of the header, the cells of each row"""

    def __init__(self):
        super().__init__()
        self.tables, self.part, self.row = [], None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table" and "data-table" in attrs:
            self.tables.append({"name": attrs["data-table"], "merged": attrs.get("data-merged", ""), "head": [],
                                "rows": []})
        elif not self.tables:
            return
        elif tag in ("thead", "tbody"):
            self.part = tag
        elif tag == "tr":
            self.row = []
            classes = attrs.get("class") or ""
            if self.part == "tbody" and "empty" not in classes:
                self.tables[-1]["rows"].append(self.row)
        elif tag == "th" and self.part == "thead":
            self.tables[-1]["head"].append(attrs.get("data-col"))
        elif tag == "td" and self.row is not None:
            self.row.append(attrs.get("data-col"))

    def handle_endtag(self, tag):
        if tag == "table" and self.tables:
            self.part = None


def tables_of(response):
    parser = Tables()
    parser.feed(response.content.decode())
    return {table["name"]: table for table in parser.tables}


class MergedColumnsTestCase(ConsoleBase):
    """The values shown below another one are columns of their own, merged by default (console/columns.js): named in
    the header, and separable"""

    def setUp(self):
        super().setUp()
        self.make_event()
        self.dev.parent = Group.objects.create(name="Base")
        self.dev.description = "The developers"
        self.dev.save()
        AccessRequest.objects.create(requester=self.user, kind="OTHER", title="tool", justification="I need it")
        ReleaseSource.objects.create(name="colima", kind=ReleaseSource.Kind.HOMEBREW_FORMULA, identifier="colima",
                                     description="Containers", is_global=True)
        FileAccessRule.objects.create(name="SSH keys", paths="/Users/*/.ssh/id_rsa", description="Keys")
        IgnoredApp.objects.create(kind=IgnoredApp.Kind.TEAMID, value="ABCDE12345", created_by=self.admin)
        User.objects.filter(pk=self.user.pk).update(email="jdoe@example.org")
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, created_by=self.admin)

    def test_every_list_names_its_merged_columns(self):
        lists = {
            "blocked-apps": ("console:events", {"app-path": "program", "last-mac": "last-at",
                                                "ignore-note": "rules"}),
            "macs": ("console:machines", {"serial": "mac", "macos": "santa"}),
            "requests": ("console:requests", {"justification": "request"}),
            "package-rules": ("console:sources", {"packages": "name", "description": "name", "policy": "rules",
                                                  "last-check": "checked"}),
            "file-access": ("console:file_access_rules", {"description": "name"}),
            "groups": ("console:groups", {"parent": "name", "description": "name"}),
            "users": ("console:admin_users", {"email": "user"}),
            "ignored-apps": ("console:admin_ignored", {"kind": "value", "created-by": "created"}),
            "rules": ("console:rules", {"comment": "identifier", "created-by": "created"}),
        }
        for name, (url_name, merges) in lists.items():
            with self.subTest(name):
                response = self.client.get(reverse(url_name), {"q": ""})
                table = tables_of(response)[name]
                merged = dict(pair.split(":") for pair in table["merged"].split())
                self.assertEqual({child: merged.get(child) for child in merges}, merges)
                for child, parent in merges.items():
                    self.assertIn(child, table["head"])
                    self.assertIn(parent, table["head"])
                # every row has a cell per column: columns.js finds them by position
                self.assertTrue(table["rows"])
                for row in table["rows"]:
                    self.assertEqual(len(row), len(table["head"]))
                # the grey rows while the rows load, as many columns too
                response = self.client.get(reverse(url_name), {"q": ""}, **NAVIGATE)
                skeleton = tables_of(response).get(name)
                if skeleton and skeleton["rows"]:
                    self.assertEqual(len(skeleton["rows"][0]), len(skeleton["head"]))

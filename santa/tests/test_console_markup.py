"""The markup of the console forms: what the browser checks (Chromium's form issues: labels, ids, autocomplete)."""
import re
from html.parser import HTMLParser

from django import forms
from django.template.loader import render_to_string
from django.urls import reverse

from santa.console.forms import UserForm
from santa.models import SavedFilter

from .test_console import ConsoleBase


class _Fields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.label_fors, self.fields = set(), [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "label" and "for" in attrs:
            self.label_fors.append(attrs["for"])
        if tag in ("input", "select", "textarea") and attrs.get("type") not in ("hidden", "submit", "button"):
            self.fields.append(attrs)


def parse(html):
    parser = _Fields()
    parser.feed(html)
    return parser


class GroupForm(forms.Form):
    name = forms.CharField()
    scope = forms.ChoiceField(choices=[("a", "A"), ("b", "B")], widget=forms.RadioSelect)
    groups = forms.MultipleChoiceField(choices=[("1", "One")], widget=forms.CheckboxSelectMultiple)


class FieldMarkupTestCase(ConsoleBase):
    def render(self, field):
        return render_to_string("console/_field.html", {"field": field})

    def test_a_group_has_no_label_for_nothing(self):
        form = GroupForm()
        for name in ("scope", "groups"):
            html = self.render(form[name])
            self.assertNotIn('for=""', html)
            self.assertIn('<span class="field-label">', html)
            # each choice keeps its own label
            self.assertTrue(all(target in parse(html).ids for target in parse(html).label_fors))

    def test_fields_of_the_console_are_not_filled_in_by_the_browser(self):
        self.assertIn('autocomplete="off"', self.render(GroupForm()["name"]))
        form = UserForm()
        # the account of someone else: not the data of the one signed in
        self.assertIn('autocomplete="off"', self.render(form["email"]))
        self.assertIn('autocomplete="new-password"', self.render(form["password1"]))

    def test_the_pages_label_existing_fields(self):
        # the views of the user have a rename field each (the views picker of the filter bar)
        for page in ("events-blocked", "rules"):
            SavedFilter.objects.create(user=self.admin, page=page, name="7 days", query="q=&days=7")
        for url in (reverse("console:rule_add"), reverse("console:source_add"), reverse("console:group_add"),
                    reverse("console:admin_user_add"), reverse("console:events"), reverse("console:rules"),
                    reverse("profile")):
            with self.subTest(url=url):
                html = self.client.get(url, {"rows": "now"}).content.decode()
                page = parse(html)
                self.assertEqual([target for target in page.label_fors if target not in page.ids], [])
                # a field the browser could fill in names whether it should
                unnamed = [field for field in page.fields if not (field.get("id") or field.get("name"))]
                self.assertEqual(unnamed, [])
                for field in page.fields:
                    if field.get("name") in ("name", "email", "username", "first_name", "last_name", "q", "tag"):
                        self.assertIn("autocomplete", field, field)

    def test_the_lists_select_all_has_an_id(self):
        self.make_event()
        html = self.client.get(reverse("console:events"), {"rows": "now"}).content.decode()
        self.assertTrue(re.search(r'<input type="checkbox" id="select-all-events[^"]*" data-select-all', html))

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from santa.models import Group, Rule, RuleType


class AddGroupsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("admin", "admin@example.com", "pw")
        cls.dev = Group.objects.create(name="Development")
        cls.sales = Group.objects.create(name="Sales")
        cls.design = Group.objects.create(name="Design")
        cls.rule_a = Rule.objects.create(rule_type=RuleType.BINARY, identifier="a" * 64)
        cls.rule_a.groups.add(cls.dev)
        cls.rule_b = Rule.objects.create(rule_type=RuleType.BINARY, identifier="b" * 64)
        cls.rule_b.groups.add(cls.sales)
        cls.global_rule = Rule.objects.create(rule_type=RuleType.TEAMID, identifier="EQHXZ8M8AV", is_global=True)

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("admin:santa_rule_changelist")

    def test_add_groups(self):
        selected = {ACTION_CHECKBOX_NAME: [self.rule_a.pk, self.rule_b.pk, self.global_rule.pk]}
        response = self.client.post(self.url, {"action": "add_groups", **selected})
        self.assertContains(response, "Add groups")
        response = self.client.post(self.url, {"action": "add_groups", "apply": "1",
                                               "groups": [self.sales.pk, self.design.pk], **selected}, follow=True)
        self.assertContains(response, "added to 2 rule(s)")
        self.assertContains(response, "1 global rule(s) skipped")
        self.assertEqual({g.name for g in self.rule_a.groups.all()}, {"Development", "Sales", "Design"})
        self.assertEqual({g.name for g in self.rule_b.groups.all()}, {"Sales", "Design"})
        self.assertFalse(self.global_rule.groups.exists())
        log_a = LogEntry.objects.get(object_id=str(self.rule_a.pk))
        self.assertIn("Groups added: Design, Sales", log_a.change_message)
        log_b = LogEntry.objects.get(object_id=str(self.rule_b.pk))
        self.assertIn("Groups added: Design", log_b.change_message)

    def test_a_group_is_required(self):
        response = self.client.post(self.url, {"action": "add_groups", "apply": "1",
                                               ACTION_CHECKBOX_NAME: [self.rule_a.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("groups", response.context["form"].errors)
        self.assertEqual(self.rule_a.groups.count(), 1)

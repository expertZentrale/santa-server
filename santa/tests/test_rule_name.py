import json

from django.urls import reverse

from santa.config_io import export_config, import_config
from santa.models import Policy, Rule, RuleType

from .test_console import SHA_A, SHA_B, ConsoleBase


class RuleNameTestCase(ConsoleBase):
    def test_create_and_show_a_named_rule(self):
        self.client.post(reverse("console:rule_add"), {
            "rule_type": RuleType.TEAMID, "identifier": "UBF8T346G9", "name": "Microsoft apps",
            "policy": Policy.ALLOWLIST, "is_global": "on", "is_enabled": "on"})
        rule = Rule.objects.get()
        self.assertEqual(rule.name, "Microsoft apps")
        # the title of the rule, a column of the list
        self.assertContains(self.client.get(reverse("console:rule", args=(rule.pk,))), "<h1>Microsoft apps</h1>",
                            html=True)
        response = self.client.get(reverse("console:rules"), {"q": ""})
        self.assertContains(response, '<td data-col="name"><span class="cell-line" title="Microsoft apps">'
                                      'Microsoft apps</span></td>', html=True)

    def test_search_by_name(self):
        named = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, name="Colima")
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_B, description="other tool")
        response = self.client.get(reverse("console:rules"), {"q": "colim"})
        self.assertEqual(list(response.context["page"]), [named])
        response = self.client.get(reverse("console:rule_suggestions"), {"q": "colim"})
        self.assertEqual(response.context["rules"], [named])
        # the rules of a Mac
        named.machines.add(self.machine)
        response = self.client.get(reverse("console:machine", args=(self.machine.pk,)), {"q": "colim"})
        self.assertEqual(list(response.context["page"]), [named])
        # the admin
        response = self.client.get(reverse("admin:santa_rule_changelist"), {"q": "colim"})
        self.assertEqual(list(response.context["cl"].result_list), [named])

    def test_csv_export(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, name="Colima", is_global=True)
        lines = self.client.get(reverse("console:rules_export")).content.decode("utf-8-sig").splitlines()
        self.assertEqual(lines[0].split(",")[:3], ["Type", "Identifier", "Name"])
        self.assertEqual(lines[1].split(",")[1:3], [SHA_A, "Colima"])

    def test_config_round_trip(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, name="Colima", is_global=True)
        data = json.loads(json.dumps(export_config()))
        self.assertEqual(data["rules"][0]["name"], "Colima")
        Rule.objects.all().delete()
        import_config(data)
        self.assertEqual(Rule.objects.get().name, "Colima")
        # a file of an older version has no names
        del data["rules"][0]["name"]
        Rule.objects.all().delete()
        import_config(data)
        self.assertEqual(Rule.objects.get().name, "")

import json

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from santa.auth import ADMIN_GROUP_NAME, REQUESTERS_GROUP_NAME
from santa.models import Group, Rule, RuleType, SignInGroup, Tag

from .test_console import SHA_A, ConsoleBase


class AdministrationTestCase(ConsoleBase):
    def test_pages_load(self):
        role = AuthGroup.objects.get(name=REQUESTERS_GROUP_NAME)
        tag = Tag.objects.create(name="homebrew")
        everyone = SignInGroup.objects.get(claim_value=SignInGroup.EVERYONE)
        self.assertRedirects(self.client.get(reverse("console:administration")), reverse("console:admin_users"))
        for name, args in [("admin_users", ()), ("admin_user", (self.user.pk,)), ("admin_user_add", ()),
                           ("admin_roles", ()), ("admin_role", (role.pk,)), ("admin_role_add", ()),
                           ("admin_sign_in_groups", ()), ("admin_sign_in_group", (everyone.pk,)),
                           ("admin_sign_in_group_add", ()), ("admin_tags", ()), ("admin_tag", (tag.pk,)),
                           ("admin_tag_add", ()), ("admin_config", ()),
                           ("admin_config_import", ())]:
            response = self.client.get(reverse(f"console:{name}", args=args))
            self.assertEqual(response.status_code, 200, name)
        self.assertContains(self.client.get(reverse("console:rules")), reverse("console:administration"))

    def test_needs_permissions(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename="view_tag"))
        self.client.force_login(viewer)
        self.assertRedirects(self.client.get(reverse("console:administration")), reverse("console:admin_tags"))
        self.assertEqual(self.client.get(reverse("console:admin_users")).status_code, 403)
        self.assertEqual(self.client.post(reverse("console:admin_tag_add"), {"name": "x"}).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("console:admin_tags")).status_code, 403)

    def test_local_account(self):
        response = self.client.post(reverse("console:admin_user_add"), {
            "username": "break-glass", "is_active": "on", "is_staff": "on",
            "roles": [AuthGroup.objects.get(name=REQUESTERS_GROUP_NAME).pk],
            "password1": "a-Long-pass-phrase-42", "password2": "a-Long-pass-phrase-42",
        })
        self.assertRedirects(response, reverse("console:admin_users"))
        user = User.objects.get(username="break-glass")
        self.assertTrue(user.is_staff and user.check_password("a-Long-pass-phrase-42"))
        self.assertTrue(user.has_perm("santa.request_other"))
        self.assertTrue(LogEntry.objects.filter(object_id=str(user.pk), action_flag=ADDITION).exists())
        response = self.client.post(reverse("console:admin_user", args=(user.pk,)), {
            "is_active": "on", "password1": "a", "password2": "b"})
        self.assertContains(response, "The two passwords are different.")

    def test_sign_in_user_keeps_the_roles_of_the_sign_in(self):
        sign_in_user = User.objects.create_user("jane@example.com")
        requesters = AuthGroup.objects.get(name=REQUESTERS_GROUP_NAME)
        by_hand = AuthGroup.objects.create(name="Tag editors")
        sign_in_user.groups.add(requesters)
        response = self.client.get(reverse("console:admin_user", args=(sign_in_user.pk,)))
        self.assertNotIn(requesters, response.context["form"].fields["roles"].queryset)
        self.assertNotIn("is_staff", response.context["form"].fields)
        self.client.post(reverse("console:admin_user", args=(sign_in_user.pk,)),
                         {"is_active": "on", "roles": [by_hand.pk]})
        self.assertEqual(set(sign_in_user.groups.all()), {requesters, by_hand})

    def test_superuser_only_changed_by_superusers(self):
        manager = User.objects.create_user("manager", is_staff=True)
        manager.user_permissions.set(Permission.objects.filter(codename__in=["view_user", "change_user"]))
        self.client.force_login(manager)
        self.assertEqual(self.client.get(reverse("console:admin_user", args=(self.admin.pk,))).status_code, 403)

    def test_cannot_deactivate_yourself(self):
        response = self.client.post(reverse("console:admin_user", args=(self.admin.pk,)), {"username": "admin"})
        self.assertContains(response, "You can&#x27;t deactivate your own account.")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_role_permissions(self):
        view_rule = Permission.objects.get(codename="view_rule")
        request_package = Permission.objects.get(codename="request_package")
        response = self.client.post(reverse("console:admin_role_add"), {
            "name": "Rule viewers", "permissions": [view_rule.pk, request_package.pk]})
        self.assertRedirects(response, reverse("console:admin_roles"))
        role = AuthGroup.objects.get(name="Rule viewers")
        self.assertEqual(set(role.permissions.all()), {view_rule, request_package})
        response = self.client.get(reverse("console:admin_role", args=(role.pk,)))
        self.assertContains(response, f'value="{view_rule.pk}" title="{view_rule.name}"')
        # the request permissions are shown on their own, not as a model row
        self.assertEqual([str(label) for label, _cell in response.context["request_permissions"]],
                         ["Request apps blocked on their Macs", "Request packages", "Request other software"])
        self.client.post(reverse("console:admin_role_delete", args=(role.pk,)))
        self.assertFalse(AuthGroup.objects.filter(pk=role.pk).exists())
        self.assertTrue(LogEntry.objects.filter(object_id=str(role.pk), action_flag=DELETION).exists())

    def test_admin_role_is_not_deleted(self):
        admins, _ = AuthGroup.objects.get_or_create(name=ADMIN_GROUP_NAME)
        self.client.post(reverse("console:admin_role_delete", args=(admins.pk,)))
        self.assertTrue(AuthGroup.objects.filter(pk=admins.pk).exists())

    def test_sign_in_group(self):
        approvers = AuthGroup.objects.create(name="Approvers")
        member = User.objects.create_user("jane@example.com")
        response = self.client.post(reverse("console:admin_sign_in_group_add"), {
            "name": "IT support", "claim_value": "11111111-2222", "console_access": "on", "roles": [approvers.pk]})
        self.assertRedirects(response, reverse("console:admin_sign_in_groups"))
        group = SignInGroup.objects.get(claim_value="11111111-2222")
        group.members.add(member)
        # a change applies to the members right away
        self.client.post(reverse("console:admin_sign_in_group", args=(group.pk,)), {
            "name": "IT support", "claim_value": "11111111-2222", "console_access": "on", "roles": [approvers.pk]})
        member.refresh_from_db()
        self.assertTrue(member.is_staff)
        self.assertIn(approvers, member.groups.all())
        self.assertTrue(LogEntry.objects.filter(object_id=str(group.pk), action_flag=CHANGE).exists())
        self.client.post(reverse("console:admin_sign_in_group_delete", args=(group.pk,)))
        member.refresh_from_db()
        self.assertFalse(member.is_staff)
        self.assertNotIn(approvers, member.groups.all())

    def test_tags(self):
        tag = Tag.objects.create(name="brew")
        rule = Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True)
        rule.tags.add(tag)
        response = self.client.get(reverse("console:admin_tags"))
        self.assertEqual(response.context["page"][0].rule_count, 1)
        self.client.post(reverse("console:admin_tag", args=(tag.pk,)), {"name": "homebrew", "description": ""})
        tag.refresh_from_db()
        self.assertEqual(tag.name, "homebrew")
        self.client.post(reverse("console:admin_tag_delete", args=(tag.pk,)))
        self.assertFalse(Tag.objects.exists())
        self.assertTrue(Rule.objects.filter(pk=rule.pk).exists())

    def test_export_and_import(self):
        Rule.objects.create(rule_type=RuleType.BINARY, identifier=SHA_A, is_global=True, description="x")
        response = self.client.get(reverse("console:admin_config_export"))
        self.assertIn("attachment", response["Content-Disposition"])
        data = json.loads(response.content)
        self.assertNotIn(self.dev.sync_token, response.content.decode())
        data["groups"].append({**data["groups"][0], "name": "Imported"})
        upload = SimpleUploadedFile("config.json", json.dumps(data).encode(), content_type="application/json")
        response = self.client.post(reverse("console:admin_config_import"), {"file": upload, "dry_run": "on"})
        self.assertContains(response, "Dry run: nothing saved")
        self.assertFalse(Group.objects.filter(name="Imported").exists())
        upload = SimpleUploadedFile("config.json", json.dumps(data).encode(), content_type="application/json")
        self.client.post(reverse("console:admin_config_import"), {"file": upload})
        self.assertTrue(Group.objects.filter(name="Imported").exists())

    def test_import_needs_permissions(self):
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.set(Permission.objects.filter(codename__in=["view_group", "view_rule",
                                                                            "view_releasesource"]))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("console:admin_config")).status_code, 200)
        upload = SimpleUploadedFile("config.json", b"{}", content_type="application/json")
        self.assertEqual(self.client.post(reverse("console:admin_config_import"), {"file": upload}).status_code, 403)

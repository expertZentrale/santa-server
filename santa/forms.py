from django import forms

from .models import Group, Policy, Rule, RuleType, Tag

ALLOW_POLICIES = [(Policy.ALLOWLIST, "Allow"), (Policy.ALLOWLIST_COMPILER, "Allow compiler")]


class ScopeForm(forms.Form):
    SCOPE_GLOBAL = "global"
    SCOPE_GROUPS = "groups"

    scope = forms.ChoiceField(
        choices=[(SCOPE_GROUPS, "Selected groups"), (SCOPE_GLOBAL, "All Macs (global)")],
        initial=SCOPE_GROUPS, widget=forms.RadioSelect,
    )
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), required=False,
                                            widget=forms.CheckboxSelectMultiple)
    description = forms.CharField(max_length=500, required=False)
    tags = forms.ModelMultipleChoiceField(queryset=Tag.objects.all(), required=False,
                                          widget=forms.CheckboxSelectMultiple,
                                          help_text="Optional, only to find the rules later.")

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("scope") == self.SCOPE_GROUPS and not cleaned_data.get("groups"):
            self.add_error("groups", "Select at least one group, or choose the global scope.")
        return cleaned_data

    def apply_scope(self, rule):
        rule.is_global = self.cleaned_data["scope"] == self.SCOPE_GLOBAL
        rule.save()
        if not rule.is_global:
            rule.groups.set(self.cleaned_data["groups"])


class AllowEventsForm(ScopeForm):
    rule_type = forms.ChoiceField(
        choices=[(RuleType.SIGNINGID, "Signing ID (all versions of this app from this developer)"),
                 (RuleType.BINARY, "This exact binary (SHA-256), for unsigned tools"),
                 (RuleType.TEAMID, "Team ID (everything signed by this developer)"),
                 (RuleType.CERTIFICATE, "Signing certificate"),
                 (RuleType.CDHASH, "CDHash")],
        initial=RuleType.SIGNINGID,
    )
    policy = forms.ChoiceField(choices=ALLOW_POLICIES, initial=Policy.ALLOWLIST)


class UploadBinaryForm(ScopeForm):
    file = forms.FileField(help_text="Mach-O binary, or a zip / tar archive (e.g. a GitHub release asset). "
                                     "The file is only hashed, it is not stored.")
    rule_type = forms.ChoiceField(
        choices=[(RuleType.SIGNINGID, "Signing ID"),
                 (RuleType.BINARY, "Binary (SHA-256 of every executable found), for unsigned tools"),
                 (RuleType.TEAMID, "Team ID"),
                 (RuleType.CERTIFICATE, "Signing certificate"),
                 (RuleType.CDHASH, "CDHash (one rule per architecture)")],
        initial=RuleType.SIGNINGID,
    )
    policy = forms.ChoiceField(choices=Policy.choices, initial=Policy.ALLOWLIST)
    binary_pattern = forms.CharField(required=False, max_length=200,
                                     help_text="Archives only: glob of the files to use, e.g. */bin/colima")


class AddGroupsForm(forms.Form):
    groups = forms.ModelMultipleChoiceField(queryset=Group.objects.all(), widget=forms.CheckboxSelectMultiple)


class TagActionForm(forms.Form):
    tag = forms.ModelChoiceField(queryset=Tag.objects.all(), required=False, help_text="An existing tag…")
    new_tag = forms.CharField(max_length=100, required=False, help_text="…or the name of a new one")

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("tag") and not cleaned_data.get("new_tag", "").strip():
            raise forms.ValidationError("Choose a tag or enter a new one.")
        return cleaned_data

    def get_tag(self):
        if self.cleaned_data.get("tag"):
            return self.cleaned_data["tag"]
        tag, _ = Tag.objects.get_or_create(name=self.cleaned_data["new_tag"].strip())
        return tag


class RuleAdminForm(forms.ModelForm):
    class Meta:
        model = Rule
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        if (not cleaned_data.get("is_global") and not cleaned_data.get("groups")
                and not cleaned_data.get("machines")):
            raise forms.ValidationError("Choose a scope: global, some groups or some machines.")
        return cleaned_data


class ImportConfigForm(forms.Form):
    file = forms.FileField(help_text="JSON file from “Export configuration” or the export_config command.")
    delete_missing = forms.BooleanField(
        required=False, help_text="Delete the manual rules and release sources that are not in the file.",
    )
    dry_run = forms.BooleanField(required=False, initial=True,
                                 help_text="Only show what would change. Uncheck to import.")

# The rules of a package rule kept the scope it had when they were created: a scope changed later never reached them
# (no group: no Mac). Once, every package rule gives its rules its scope again; from now on saving it does
# (releases.apply_source_scope). Only the changed rules are touched, in chunks (SQL Server: 2100 parameters).

from django.db import migrations
from django.utils import timezone

CHUNK = 1000
LINK_BATCH = 500


def chunked(values):
    values = list(values)
    for start in range(0, len(values), CHUNK):
        yield values[start:start + CHUNK]


def apply_scopes(apps, schema_editor):
    ReleaseSource = apps.get_model("santa", "ReleaseSource")
    Rule = apps.get_model("santa", "Rule")
    links = Rule.groups.through.objects
    for source in ReleaseSource.objects.all():
        group_ids = set(source.groups.values_list("pk", flat=True))
        rule_pks = list(Rule.objects.filter(release_source=source).values_list("pk", flat=True))
        changed = set()
        for chunk in chunked(rule_pks):
            changed.update(Rule.objects.filter(pk__in=chunk).exclude(is_global=source.is_global)
                           .values_list("pk", flat=True))
            groups_of = {pk: set() for pk in chunk}
            for rule_id, group_id in links.filter(rule_id__in=chunk).values_list("rule_id", "group_id"):
                groups_of[rule_id].add(group_id)
            changed.update(pk for pk, groups in groups_of.items() if groups != group_ids)
        for chunk in chunked(sorted(changed)):
            Rule.objects.filter(pk__in=chunk).update(is_global=source.is_global, updated_at=timezone.now())
            links.filter(rule_id__in=chunk).delete()
            links.bulk_create([links.model(rule_id=pk, group_id=group_id) for pk in chunk for group_id in group_ids],
                              batch_size=LINK_BATCH)


class Migration(migrations.Migration):

    dependencies = [
        ('santa', '0042_list_default'),
    ]

    operations = [
        migrations.RunPython(apply_scopes, migrations.RunPython.noop),
    ]

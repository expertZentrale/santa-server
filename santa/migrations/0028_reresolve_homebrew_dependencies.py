from django.db import migrations


def reresolve_homebrew_dependencies(apps, schema_editor):
    """The older Homebrew versions got the dependencies of the current formula (or none): resolved again at the next
    sync, in the versions their bottles were built with. Only the versions of the registry have a date; the current
    one (formulae.brew.sh) has none, its dependencies are right."""
    ReleaseVersion = apps.get_model("santa", "ReleaseVersion")
    ReleaseVersion.objects.filter(source__kind="HOMEBREW_FORMULA", source__include_dependencies=True,
                                  published_at__isnull=False).update(dependencies_resolved=False)


class Migration(migrations.Migration):
    dependencies = [
        ("santa", "0027_user_notification"),
    ]

    operations = [
        migrations.RunPython(reresolve_homebrew_dependencies, migrations.RunPython.noop),
    ]

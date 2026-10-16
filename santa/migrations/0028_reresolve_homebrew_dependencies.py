from django.db import migrations


def reresolve_homebrew_dependencies(apps, schema_editor):
    """The older Homebrew versions got the dependencies of the current formula (or none): resolved again at the next
    sync, in the versions their bottles were built with. All of them: a version recorded as the current one has no
    date and keeps none when newer ones come; the version current now gets the current formula again
    (releases._release_of_version)."""
    ReleaseVersion = apps.get_model("santa", "ReleaseVersion")
    ReleaseVersion.objects.filter(source__kind="HOMEBREW_FORMULA", source__include_dependencies=True).update(
        dependencies_resolved=False)


class Migration(migrations.Migration):
    dependencies = [
        ("santa", "0027_user_notification"),
    ]

    operations = [
        migrations.RunPython(reresolve_homebrew_dependencies, migrations.RunPython.noop),
    ]

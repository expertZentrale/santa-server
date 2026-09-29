from django.contrib.auth.management import create_permissions
from django.db import migrations
from django.db.models import Q

REQUEST_PERMISSIONS = ["request_event", "request_package", "request_other"]


def create_default_roles(apps, schema_editor):
    # the permissions are only created after the migrations (post_migrate), the roles need them now
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None
    AuthGroup = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    SignInGroup = apps.get_model("santa", "SignInGroup")
    User = apps.get_model("auth", "User")

    # everyone could make requests before the request permissions: keep it that way until an admin changes it
    requesters, _ = AuthGroup.objects.get_or_create(name="Santa requesters")
    requesters.permissions.add(*Permission.objects.filter(content_type__app_label="santa",
                                                          codename__in=REQUEST_PERMISSIONS))
    everyone, _ = SignInGroup.objects.get_or_create(claim_value="*", defaults={"name": "Everyone"})
    everyone.roles.add(requesters)
    user_ids = list(User.objects.values_list("pk", flat=True))
    # SQL Server: at most 2100 parameters per query
    for start in range(0, len(user_ids), 500):
        requesters.user_set.add(*user_ids[start:start + 500])

    # the admins also manage the new models, the users and the roles
    admins = AuthGroup.objects.filter(name="Santa admins").first()
    if admins:
        admins.permissions.add(*Permission.objects.filter(
            Q(content_type__app_label="santa") | Q(content_type__app_label="auth",
                                                   content_type__model__in=["user", "group"])))


class Migration(migrations.Migration):

    dependencies = [
        ("santa", "0011_sign_in_groups_and_request_permissions"),
    ]

    operations = [
        migrations.RunPython(create_default_roles, migrations.RunPython.noop),
    ]

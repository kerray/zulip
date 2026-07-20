from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def reset_enable_web_push_notifications_to_off(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """The deployed 0802 shipped this setting on by default (db_default=True),
    but the reviewed web push series makes it opt-in (default False): the
    setting is meaningless until the user has granted browser notification
    permission and registered a per-browser subscription. Reset every existing
    row to False so the deployed database matches the opt-in semantics of the
    revised series; users re-enable web push explicitly (the client flow
    requires it anyway).
    """
    UserProfile = apps.get_model("zerver", "UserProfile")
    RealmUserDefault = apps.get_model("zerver", "RealmUserDefault")

    UserProfile.objects.filter(enable_web_push_notifications=True).update(
        enable_web_push_notifications=False
    )
    RealmUserDefault.objects.filter(enable_web_push_notifications=True).update(
        enable_web_push_notifications=False
    )


def dedupe_web_push_subscriptions_by_endpoint(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """The deployed 0803 made (user, endpoint) unique, but the revised series
    makes the endpoint globally unique: an endpoint identifies a single browser
    install, so on a shared browser re-subscribing must transfer the row rather
    than leave a second row that would leak the previous user's notifications.
    Before we can add the global-endpoint constraint, collapse any rows that
    share an endpoint down to the most recently updated one.
    """
    WebPushSubscription = apps.get_model("zerver", "WebPushSubscription")

    keep_ids = (
        WebPushSubscription.objects.order_by("endpoint", "-last_updated", "-id")
        .distinct("endpoint")
        .values_list("id", flat=True)
    )
    WebPushSubscription.objects.exclude(id__in=list(keep_ids)).delete()


class Migration(migrations.Migration):
    """Reconcile the deployed web push schema (0802 + 0803, applied on the live
    server from the earlier backport) with the reviewed web push series, so the
    migration state matches the model state. A fresh install runs
    0802 -> 0803 -> 0804 and lands on the same final schema as upstream's
    0807 + 0808.
    """

    dependencies = [
        ("zerver", "0803_webpushsubscription"),
    ]

    operations = [
        migrations.AlterField(
            model_name="realmuserdefault",
            name="enable_web_push_notifications",
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AlterField(
            model_name="userprofile",
            name="enable_web_push_notifications",
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.RunPython(
            reset_enable_web_push_notifications_to_off,
            reverse_code=migrations.RunPython.noop,
            elidable=True,
        ),
        migrations.RunPython(
            dedupe_web_push_subscriptions_by_endpoint,
            reverse_code=migrations.RunPython.noop,
            elidable=True,
        ),
        migrations.RemoveConstraint(
            model_name="webpushsubscription",
            name="unique_web_push_subscription_user_endpoint",
        ),
        migrations.AddConstraint(
            model_name="webpushsubscription",
            constraint=models.UniqueConstraint(
                fields=["endpoint"],
                name="unique_web_push_subscription_endpoint",
            ),
        ),
    ]

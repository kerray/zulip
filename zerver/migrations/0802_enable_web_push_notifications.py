from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("zerver", "0801_realmexport_backfill_export_from_prior_server_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="realmuserdefault",
            name="enable_web_push_notifications",
            field=models.BooleanField(default=True, db_default=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="enable_web_push_notifications",
            field=models.BooleanField(default=True, db_default=True),
        ),
    ]

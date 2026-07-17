from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("zerver", "0806_stream_default_push_notifications"),
    ]

    operations = [
        migrations.AddField(
            model_name="realmuserdefault",
            name="enable_web_push_notifications",
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="enable_web_push_notifications",
            field=models.BooleanField(default=False, db_default=False),
        ),
    ]

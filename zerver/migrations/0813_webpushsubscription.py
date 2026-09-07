import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("zerver", "0812_enable_web_push_notifications"),
    ]

    operations = [
        migrations.CreateModel(
            name="WebPushSubscription",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
                ("endpoint", models.TextField()),
                ("p256dh_key", models.CharField(max_length=255)),
                ("auth_secret", models.CharField(max_length=255)),
                ("user_agent", models.CharField(default="", max_length=255)),
                ("date_created", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_updated", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=["endpoint"],
                        name="unique_web_push_subscription_endpoint",
                    )
                ],
            },
        ),
    ]

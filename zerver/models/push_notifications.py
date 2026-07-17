from django.db import models
from django.db.models import CASCADE, F, Q
from django.db.models.functions import Lower
from django.utils.timezone import now as timezone_now

from zerver.models.users import UserProfile


class AbstractPushDeviceToken(models.Model):
    APNS = 1
    FCM = 2

    KINDS = (
        (APNS, "apns"),
        # The string value in the database is "gcm" for legacy reasons.
        # TODO: We should migrate it.
        (FCM, "gcm"),
    )

    kind = models.PositiveSmallIntegerField(choices=KINDS)

    # The token is a unique device-specific token that is
    # sent to us from each device:
    #   - APNS token if kind == APNS
    #   - FCM registration id if kind == FCM
    token = models.CharField(max_length=4096, db_index=True)

    # TODO: last_updated should be renamed date_created, since it is
    # no longer maintained as a last_updated value.
    last_updated = models.DateTimeField(auto_now=True)

    # [optional] Contains the app id of the device if it is an iOS device
    ios_app_id = models.TextField(null=True)

    class Meta:
        abstract = True


class PushDeviceToken(AbstractPushDeviceToken):
    # The user whose device this is
    user = models.ForeignKey(UserProfile, db_index=True, on_delete=CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "user_id",
                "kind",
                Lower(F("token")),
                name="zerver_pushdevicetoken_apns_user_kind_token",
                condition=Q(kind=AbstractPushDeviceToken.APNS),
            ),
            models.UniqueConstraint(
                "user_id",
                "kind",
                "token",
                name="zerver_pushdevicetoken_fcm_user_kind_token",
                condition=Q(kind=AbstractPushDeviceToken.FCM),
            ),
        ]


class WebPushSubscription(models.Model):
    """A browser Web Push (RFC 8030/8291) subscription for a user.

    Unlike Device (mobile/bouncer E2EE push), these notifications are
    delivered directly from this server to the browser's push service
    using VAPID; there is no push notification bouncer involved.
    """

    user = models.ForeignKey(UserProfile, on_delete=CASCADE)
    # The push service URL (PushSubscription.endpoint). FCM and Mozilla
    # endpoints can exceed 255 characters, so this is a TextField. It is
    # globally unique (not just per-user): the endpoint identifies a single
    # browser install, so on a shared browser re-subscribing must transfer the
    # row to the new user rather than leaving a second row that would leak the
    # previous user's notification content to the current session.
    endpoint = models.TextField()
    # The base64url public key (p256dh) and auth secret from the browser's
    # PushSubscription; pywebpush uses them to encrypt each payload.
    p256dh_key = models.CharField(max_length=255)
    auth_secret = models.CharField(max_length=255)
    # User-Agent captured at registration, to identify which browser a
    # subscription came from when debugging delivery failures.
    user_agent = models.CharField(max_length=255, default="")
    date_created = models.DateTimeField(default=timezone_now)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint"],
                name="unique_web_push_subscription_endpoint",
            ),
        ]

import base64
from unittest import mock

import orjson
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import override_settings
from pywebpush import WebPushException
from typing_extensions import override

from zerver.actions.message_send import get_recipient_info
from zerver.actions.user_settings import do_change_user_setting
from zerver.lib.avatar import get_avatar_for_inaccessible_user
from zerver.lib.outgoing_http import OutgoingSession
from zerver.lib.push_notifications import (
    WEB_PUSH_REQUEST_TIMEOUT_SECONDS,
    handle_push_notification,
    has_webpush_credentials,
    push_notifications_configured,
    send_web_push_notifications,
)
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.web_push_vapid import derive_vapid_public_key
from zerver.models.push_notifications import PushDeviceToken, WebPushSubscription
from zerver.models.recipients import get_or_create_direct_message_group
from zerver.models.scheduled_jobs import NotificationTriggers
from zerver.models.users import UserProfile

EXAMPLE_ENDPOINT = "https://fcm.googleapis.com/fcm/send/example-endpoint"
# A valid p256dh key is the unpadded base64url encoding of the 65-byte
# uncompressed P-256 public point (leading 0x04); auth is 16 random bytes.
EXAMPLE_P256DH_KEY = (
    "BM0RXeBm6BH-5Q_T43um3GJzYxGKpa6yJto75AAb1MnPHds6N_Q_68YezVFcBceNwHKF3ktRjE9ALS8sKClqByg"
)
ALT_P256DH_KEY = (
    "BJ3KiGkcMGe5xN-4F9LKcFC6FDAyWyBCQU8y8nTHgMEIOSKLgbcRY5S6Lc_oeuh7Q2DRLQWr5WRJs2U1xAsc-jE"
)
EXAMPLE_AUTH_SECRET = "L6IFAjK30BVmUrxYW-1vUw"
EXAMPLE_SUBSCRIPTION = {
    "endpoint": EXAMPLE_ENDPOINT,
    "p256dh_key": EXAMPLE_P256DH_KEY,
    "auth_secret": EXAMPLE_AUTH_SECRET,
    "user_agent": "Mozilla/5.0",
}


def generate_vapid_private_key_b64() -> str:
    """Return a fresh VAPID private key in the base64url PKCS#8 DER secret form."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    der = private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return base64.urlsafe_b64encode(der).rstrip(b"=").decode()


class WebPushVapidTest(ZulipTestCase):
    def test_has_webpush_credentials(self) -> None:
        key = generate_vapid_private_key_b64()
        with override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=None):
            self.assertFalse(has_webpush_credentials())
        with override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=key):
            self.assertTrue(has_webpush_credentials())
        # generate-secrets seeds a keypair on every server, so a present
        # secret must not by itself activate web push.
        with override_settings(WEB_PUSH_ENABLED=False, WEB_PUSH_VAPID_PRIVATE_KEY=key):
            self.assertFalse(has_webpush_credentials())

    def test_derive_vapid_public_key(self) -> None:
        public_key_b64 = derive_vapid_public_key(generate_vapid_private_key_b64())
        # The applicationServerKey is the unpadded base64url uncompressed
        # P-256 point: 65 bytes starting with the 0x04 uncompressed marker.
        padding = "=" * (-len(public_key_b64) % 4)
        point = base64.urlsafe_b64decode(public_key_b64 + padding)
        self.assert_length(point, 65)
        self.assertEqual(point[0], 0x04)

    def test_secret_form_is_parseable_by_py_vapid(self) -> None:
        """The send path hands the secret's base64url PKCS#8 DER form directly
        to pywebpush, whose py_vapid urlsafe-b64decodes it — this must keep
        working without any PEM round-trip (which py_vapid silently corrupts),
        and must sign with the same key browsers subscribed against."""
        from py_vapid import Vapid, b64urlencode

        private_key_b64 = generate_vapid_private_key_b64()
        vapid = Vapid.from_string(private_key=private_key_b64)
        assert vapid.public_key is not None
        point = vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.assertEqual(b64urlencode(point), derive_vapid_public_key(private_key_b64))


class WebPushSubscriptionEndpointTest(ZulipTestCase):
    ENDPOINT = "/api/v1/users/me/web_push_subscriptions"

    def test_register_subscription(self) -> None:
        user = self.example_user("hamlet")
        self.assertEqual(WebPushSubscription.objects.count(), 0)

        result = self.api_post(user, self.ENDPOINT, EXAMPLE_SUBSCRIPTION)
        self.assert_json_success(result)

        subscription = WebPushSubscription.objects.get(user=user)
        self.assertEqual(subscription.endpoint, EXAMPLE_ENDPOINT)
        self.assertEqual(subscription.p256dh_key, EXAMPLE_SUBSCRIPTION["p256dh_key"])
        self.assertEqual(subscription.auth_secret, EXAMPLE_SUBSCRIPTION["auth_secret"])
        self.assertEqual(subscription.user_agent, "Mozilla/5.0")

    def test_register_subscription_is_idempotent(self) -> None:
        user = self.example_user("hamlet")
        self.api_post(user, self.ENDPOINT, EXAMPLE_SUBSCRIPTION)

        # Re-registering the same endpoint refreshes the keys in place
        # rather than creating a duplicate row.
        updated = {**EXAMPLE_SUBSCRIPTION, "p256dh_key": ALT_P256DH_KEY}
        result = self.api_post(user, self.ENDPOINT, updated)
        self.assert_json_success(result)

        self.assertEqual(WebPushSubscription.objects.filter(user=user).count(), 1)
        subscription = WebPushSubscription.objects.get(user=user)
        self.assertEqual(subscription.p256dh_key, ALT_P256DH_KEY)

    def test_register_multiple_endpoints(self) -> None:
        user = self.example_user("hamlet")
        self.api_post(user, self.ENDPOINT, EXAMPLE_SUBSCRIPTION)
        second = {**EXAMPLE_SUBSCRIPTION, "endpoint": EXAMPLE_ENDPOINT + "-2"}
        self.api_post(user, self.ENDPOINT, second)

        self.assertEqual(WebPushSubscription.objects.filter(user=user).count(), 2)

    def test_endpoint_is_globally_unique_and_transfers_owner(self) -> None:
        # A push endpoint identifies one browser install. If user A subscribes
        # it and then user B logs into the same browser and subscribes it, the
        # single row must transfer to B — otherwise A's row would still exist
        # and A's notification content would be delivered to B's session.
        hamlet = self.example_user("hamlet")
        cordelia = self.example_user("cordelia")

        self.api_post(hamlet, self.ENDPOINT, EXAMPLE_SUBSCRIPTION)
        transferred = {**EXAMPLE_SUBSCRIPTION, "p256dh_key": ALT_P256DH_KEY}
        self.api_post(cordelia, self.ENDPOINT, transferred)

        self.assertEqual(WebPushSubscription.objects.filter(endpoint=EXAMPLE_ENDPOINT).count(), 1)
        self.assertEqual(WebPushSubscription.objects.filter(user=hamlet).count(), 0)
        subscription = WebPushSubscription.objects.get(endpoint=EXAMPLE_ENDPOINT)
        self.assertEqual(subscription.user_id, cordelia.id)
        self.assertEqual(subscription.p256dh_key, ALT_P256DH_KEY)

    def test_subscriptions_are_capped_per_user(self) -> None:
        from zerver.actions.web_push import MAX_WEB_PUSH_SUBSCRIPTIONS_PER_USER

        user = self.example_user("hamlet")
        # Register one more than the cap; the oldest must be evicted rather
        # than the request erroring, so the count settles at the cap.
        total = MAX_WEB_PUSH_SUBSCRIPTIONS_PER_USER + 1
        for i in range(total):
            payload = {**EXAMPLE_SUBSCRIPTION, "endpoint": f"{EXAMPLE_ENDPOINT}-{i}"}
            self.assert_json_success(self.api_post(user, self.ENDPOINT, payload))

        self.assertEqual(
            WebPushSubscription.objects.filter(user=user).count(),
            MAX_WEB_PUSH_SUBSCRIPTIONS_PER_USER,
        )
        # The first-registered (oldest) endpoint is the one evicted.
        self.assertFalse(
            WebPushSubscription.objects.filter(endpoint=f"{EXAMPLE_ENDPOINT}-0").exists()
        )
        self.assertTrue(
            WebPushSubscription.objects.filter(endpoint=f"{EXAMPLE_ENDPOINT}-{total - 1}").exists()
        )

    def test_eviction_does_not_delete_transferred_row(self) -> None:
        # Eviction snapshots the stale subscription IDs, then deletes them.
        # Between snapshot and delete, a concurrent registration can transfer
        # one of those rows to another user (endpoints are globally unique and
        # re-subscribing transfers ownership). Deleting by ID alone would then
        # destroy the new owner's live subscription, so the eviction delete is
        # ownership-conditional (adds `user=user_profile`). This calls the very
        # helper the production registration uses, so dropping the `user=`
        # predicate from it would fail here: a stale-ID list that includes a row
        # now owned by someone else must leave that row intact while still
        # evicting our own.
        from zerver.actions.web_push import _evict_stale_subscriptions

        hamlet = self.example_user("hamlet")
        cordelia = self.example_user("cordelia")

        hamlet_sub = WebPushSubscription.objects.create(
            user=hamlet,
            endpoint=f"{EXAMPLE_ENDPOINT}-hamlet",
            p256dh_key=EXAMPLE_P256DH_KEY,
            auth_secret=EXAMPLE_AUTH_SECRET,
        )
        # cordelia_sub stands in for a row that was hamlet's when the stale-ID
        # list was snapshotted but was transferred to cordelia before the
        # eviction delete ran.
        cordelia_sub = WebPushSubscription.objects.create(
            user=cordelia,
            endpoint=f"{EXAMPLE_ENDPOINT}-transferred",
            p256dh_key=ALT_P256DH_KEY,
            auth_secret=EXAMPLE_AUTH_SECRET,
        )

        # Drive the production eviction helper with a stale-ID list snapshotted
        # while both rows looked like hamlet's, cordelia_sub having since been
        # transferred.
        _evict_stale_subscriptions(hamlet, [hamlet_sub.id, cordelia_sub.id])

        # hamlet's own row is evicted; the transferred row survives untouched.
        self.assertFalse(WebPushSubscription.objects.filter(id=hamlet_sub.id).exists())
        self.assertTrue(WebPushSubscription.objects.filter(id=cordelia_sub.id).exists())
        self.assertEqual(WebPushSubscription.objects.get(id=cordelia_sub.id).user_id, cordelia.id)

    def test_remove_subscription(self) -> None:
        user = self.example_user("hamlet")
        self.api_post(user, self.ENDPOINT, EXAMPLE_SUBSCRIPTION)
        self.assertEqual(WebPushSubscription.objects.filter(user=user).count(), 1)

        result = self.api_delete(user, self.ENDPOINT, {"endpoint": EXAMPLE_ENDPOINT})
        self.assert_json_success(result)
        self.assertEqual(WebPushSubscription.objects.filter(user=user).count(), 0)

    def test_remove_nonexistent_subscription_is_noop(self) -> None:
        user = self.example_user("hamlet")
        result = self.api_delete(user, self.ENDPOINT, {"endpoint": EXAMPLE_ENDPOINT})
        self.assert_json_success(result)

    def test_bot_cannot_subscribe(self) -> None:
        bot = self.example_user("default_bot")
        result = self.api_post(bot, self.ENDPOINT, EXAMPLE_SUBSCRIPTION)
        self.assert_json_error(result, "This endpoint does not accept bot requests.")
        self.assertEqual(WebPushSubscription.objects.count(), 0)

    def test_non_https_endpoint_rejected(self) -> None:
        user = self.example_user("hamlet")
        payload = {**EXAMPLE_SUBSCRIPTION, "endpoint": "http://push.example.com/insecure"}
        result = self.api_post(user, self.ENDPOINT, payload)
        self.assert_json_error(result, "Invalid endpoint: Value error, Not an https URL")
        self.assertEqual(WebPushSubscription.objects.count(), 0)

    def test_malformed_p256dh_key_rejected(self) -> None:
        user = self.example_user("hamlet")
        # Not base64url, wrong length, a valid-length point without the 0x04
        # uncompressed marker, and a 65-byte 0x04-prefixed blob that is not
        # actually on the P-256 curve must all be rejected with a clean 400.
        bad_uncompressed = "A" + EXAMPLE_P256DH_KEY[1:]
        off_curve = base64.urlsafe_b64encode(b"\x04" + b"\x00" * 64).rstrip(b"=").decode()
        for bad_key in ("not base64!!", "c2hvcnQ", bad_uncompressed, off_curve):
            payload = {**EXAMPLE_SUBSCRIPTION, "p256dh_key": bad_key}
            result = self.api_post(user, self.ENDPOINT, payload)
            self.assert_json_error(result, "Invalid p256dh_key: Value error, Invalid p256dh key")
        self.assertEqual(WebPushSubscription.objects.count(), 0)

    def test_malformed_auth_secret_rejected(self) -> None:
        user = self.example_user("hamlet")
        # Auth secret must decode to exactly 16 bytes.
        for bad_secret in ("not base64!!", "dG9vc2hvcnQ"):
            payload = {**EXAMPLE_SUBSCRIPTION, "auth_secret": bad_secret}
            result = self.api_post(user, self.ENDPOINT, payload)
            self.assert_json_error(result, "Invalid auth_secret: Value error, Invalid auth secret")
        self.assertEqual(WebPushSubscription.objects.count(), 0)

    def test_unauthenticated_request_rejected(self) -> None:
        result = self.client_post(self.ENDPOINT, EXAMPLE_SUBSCRIPTION)
        self.assert_json_error(
            result, "Not logged in: API authentication or user session required", status_code=401
        )


class WebPushSendTest(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.vapid_key = generate_vapid_private_key_b64()
        self.user = self.example_user("hamlet")
        self.sender = self.example_user("iago")
        # Web push is opt-in (defaults off), so enable it for the user under
        # test; individual tests that need it off override this explicitly.
        do_change_user_setting(self.user, "enable_web_push_notifications", True, acting_user=None)

    def create_subscription(self, endpoint: str = EXAMPLE_ENDPOINT) -> WebPushSubscription:
        return WebPushSubscription.objects.create(
            user=self.user,
            endpoint=endpoint,
            p256dh_key=EXAMPLE_SUBSCRIPTION["p256dh_key"],
            auth_secret=EXAMPLE_SUBSCRIPTION["auth_secret"],
            user_agent=EXAMPLE_SUBSCRIPTION["user_agent"],
        )

    def send_dm_and_handle(self, content: str = "hello there") -> tuple[int, mock.MagicMock]:
        message_id = self.send_personal_message(self.sender, self.user, content)
        missed_message = {
            "message_id": message_id,
            "trigger": NotificationTriggers.DIRECT_MESSAGE,
        }
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            self.assertLogs("zerver.lib.push_notifications", level="INFO"),
        ):
            handle_push_notification(self.user.id, missed_message)
        return message_id, mock_webpush

    def test_send_web_push_notifications_calls_webpush(self) -> None:
        subscription = self.create_subscription()
        payload = {"type": "message", "message_id": 1}
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
        ):
            send_web_push_notifications(self.user, payload, [subscription])

        mock_webpush.assert_called_once()
        args, kwargs = mock_webpush.call_args
        self.assertEqual(args[0]["endpoint"], subscription.endpoint)
        self.assertEqual(args[0]["keys"]["p256dh"], subscription.p256dh_key)
        self.assertEqual(args[0]["keys"]["auth"], subscription.auth_secret)
        self.assertEqual(orjson.loads(args[1])["type"], "message")
        # Production hands pywebpush the secret's native base64url PKCS#8 DER
        # form (what py_vapid expects), not a PEM; assert we forwarded exactly
        # the configured secret.
        self.assertEqual(kwargs["vapid_private_key"], self.vapid_key)
        self.assertTrue(kwargs["vapid_claims"]["sub"].startswith("mailto:"))

    def test_send_to_each_subscription(self) -> None:
        self.create_subscription(EXAMPLE_ENDPOINT)
        self.create_subscription(EXAMPLE_ENDPOINT + "-2")
        subscriptions = list(WebPushSubscription.objects.filter(user=self.user))
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
        ):
            send_web_push_notifications(self.user, {"type": "message"}, subscriptions)
        self.assertEqual(mock_webpush.call_count, 2)

    def test_gone_subscription_is_pruned(self) -> None:
        subscription = self.create_subscription()
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            self.assertLogs("zerver.lib.push_notifications", level="INFO"),
        ):
            mock_webpush.side_effect = WebPushException("Gone", response=mock.Mock(status_code=410))
            send_web_push_notifications(self.user, {"type": "message"}, [subscription])
        self.assertFalse(WebPushSubscription.objects.filter(id=subscription.id).exists())

    def test_other_error_keeps_subscription(self) -> None:
        subscription = self.create_subscription()
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            self.assertLogs("zerver.lib.push_notifications", level="WARNING"),
        ):
            mock_webpush.side_effect = WebPushException(
                "Server error", response=mock.Mock(status_code=500)
            )
            # The failure must not escape the loop.
            send_web_push_notifications(self.user, {"type": "message"}, [subscription])
        self.assertTrue(WebPushSubscription.objects.filter(id=subscription.id).exists())

    def test_encryption_error_is_isolated(self) -> None:
        # Malformed key material makes pywebpush raise (e.g. ValueError) while
        # encrypting, outside WebPushException. That must be caught per
        # subscription so it never crashes the worker's queue job, and the
        # subscription is kept (it is not a delivery failure).
        subscription = self.create_subscription()
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            self.assertLogs("zerver.lib.push_notifications", level="WARNING"),
        ):
            mock_webpush.side_effect = ValueError("Invalid EC point")
            send_web_push_notifications(self.user, {"type": "message"}, [subscription])
        self.assertTrue(WebPushSubscription.objects.filter(id=subscription.id).exists())

    def test_connection_error_keeps_subscription(self) -> None:
        # A network failure talking to the push service (pywebpush forwards it
        # from requests) is a transient delivery error, not a dead endpoint:
        # log it and keep the subscription so a later send can retry.
        subscription = self.create_subscription()
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            self.assertLogs("zerver.lib.push_notifications", level="WARNING") as logs,
        ):
            mock_webpush.side_effect = requests.ConnectionError("push service unreachable")
            send_web_push_notifications(self.user, {"type": "message"}, [subscription])
        self.assertTrue(WebPushSubscription.objects.filter(id=subscription.id).exists())
        self.assertIn("Web push connection error", logs.output[0])

    def test_webpush_uses_proxy_session_and_bounded_timeout(self) -> None:
        # Requests go through the Smokescreen-aware outgoing session (SSRF
        # guard) with a bounded timeout so a hung push service can't wedge the
        # notification worker.
        subscription = self.create_subscription()
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
        ):
            send_web_push_notifications(self.user, {"type": "message"}, [subscription])
        _args, kwargs = mock_webpush.call_args
        self.assertIsInstance(kwargs["requests_session"], OutgoingSession)
        self.assertEqual(kwargs["timeout"], WEB_PUSH_REQUEST_TIMEOUT_SECONDS)

    def test_handle_push_notification_sends_web_push(self) -> None:
        # Web push must work even though no mobile push is configured.
        self.assertFalse(push_notifications_configured())
        self.create_subscription()
        message_id, mock_webpush = self.send_dm_and_handle()

        mock_webpush.assert_called_once()
        data = orjson.loads(mock_webpush.call_args[0][1])
        self.assertEqual(data["type"], "message")
        self.assertEqual(data["message_id"], message_id)
        self.assertEqual(data["title"], self.sender.full_name)
        self.assertEqual(data["realm_name"], self.user.realm.name)
        # The deep link is built server-side (an absolute `near/<id>` narrow
        # URL); the service worker just navigates to it.
        self.assertTrue(data["url"].startswith(f"{self.user.realm.url}/#narrow/dm/"))
        self.assertTrue(data["url"].endswith(f"/near/{message_id}"))
        self.assertNotIn("narrow", data)

    def send_channel_message_and_handle(
        self, content: str = "hello there", topic_name: str = "web push"
    ) -> tuple[int, mock.MagicMock]:
        self.subscribe(self.user, "Denmark")
        message_id = self.send_stream_message(
            self.sender, "Denmark", content, topic_name=topic_name
        )
        missed_message = {
            "message_id": message_id,
            "trigger": NotificationTriggers.MENTION,
        }
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            self.assertLogs("zerver.lib.push_notifications", level="INFO"),
        ):
            handle_push_notification(self.user.id, missed_message)
        return message_id, mock_webpush

    def test_channel_message_web_push_payload(self) -> None:
        # A channel message titles the notification "#channel > topic" and tags
        # it by channel/topic so the browser coalesces per conversation.
        self.create_subscription()
        message_id, mock_webpush = self.send_channel_message_and_handle(topic_name="my topic")
        mock_webpush.assert_called_once()
        data = orjson.loads(mock_webpush.call_args[0][1])
        self.assertEqual(data["message_id"], message_id)
        self.assertEqual(data["title"], "#Denmark > my topic")
        self.assertTrue(data["tag"].startswith("channel:"))
        self.assertTrue(data["tag"].endswith(":my topic"))
        self.assertTrue(data["url"].startswith(f"{self.user.realm.url}/#narrow/channel/"))

    def test_channel_message_inaccessible_sender_redacts_icon(self) -> None:
        # A guest who can't access the sender (a public-stream message from
        # someone outside their can_access_all_users_group) gets the generic
        # inaccessible-user avatar instead of the sender's real one.
        self.create_subscription()
        with mock.patch("zerver.lib.push_notifications.check_can_access_user", return_value=False):
            _message_id, mock_webpush = self.send_channel_message_and_handle()
        data = orjson.loads(mock_webpush.call_args[0][1])
        self.assertEqual(data["icon"], get_avatar_for_inaccessible_user())

    def test_disabled_setting_skips_web_push(self) -> None:
        do_change_user_setting(self.user, "enable_web_push_notifications", False, acting_user=None)
        self.create_subscription()
        _message_id, mock_webpush = self.send_dm_and_handle()
        mock_webpush.assert_not_called()

    def test_no_subscription_skips_web_push(self) -> None:
        _message_id, mock_webpush = self.send_dm_and_handle()
        mock_webpush.assert_not_called()

    def test_no_vapid_configured_skips(self) -> None:
        self.create_subscription()
        message_id = self.send_personal_message(self.sender, self.user, "hello")
        missed_message = {
            "message_id": message_id,
            "trigger": NotificationTriggers.DIRECT_MESSAGE,
        }
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=None),
            mock.patch("pywebpush.webpush") as mock_webpush,
        ):
            # No mobile push and no VAPID: the worker guard returns early.
            handle_push_notification(self.user.id, missed_message)
        mock_webpush.assert_not_called()

    def test_dm_content_redacted_when_disabled(self) -> None:
        do_change_user_setting(
            self.user, "pm_content_in_desktop_notifications", False, acting_user=None
        )
        self.create_subscription()
        _message_id, mock_webpush = self.send_dm_and_handle(content="a secret plan")
        data = orjson.loads(mock_webpush.call_args[0][1])
        self.assertEqual(data["body"], "New direct message")
        self.assertNotIn("secret", data["body"])

    def test_payload_stays_under_size_limit(self) -> None:
        self.create_subscription()
        _message_id, mock_webpush = self.send_dm_and_handle(content="lorem ipsum " * 500)
        serialized = mock_webpush.call_args[0][1]
        self.assertLess(len(serialized), 3500)

    def test_web_only_user_skips_mobile_but_gets_web(self) -> None:
        # Mobile push off, web push on, and the user ALSO has a mobile device
        # registered. A DM is an offline-push-gated trigger, so it must fire
        # web push only and never touch the mobile device.
        do_change_user_setting(
            self.user, "enable_offline_push_notifications", False, acting_user=None
        )
        do_change_user_setting(
            self.user, "enable_online_push_notifications", False, acting_user=None
        )
        self.create_subscription()
        PushDeviceToken.objects.create(
            user=self.user, token="mobile-token", kind=PushDeviceToken.FCM
        )
        message_id = self.send_personal_message(self.sender, self.user, "hello")
        missed_message = {
            "message_id": message_id,
            "trigger": NotificationTriggers.DIRECT_MESSAGE,
        }
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            mock.patch(
                "zerver.lib.push_notifications.send_push_notifications_legacy"
            ) as mock_legacy,
            self.assertLogs("zerver.lib.push_notifications", level="INFO"),
        ):
            handle_push_notification(self.user.id, missed_message)
        mock_webpush.assert_called_once()
        mock_legacy.assert_not_called()

    def test_mobile_user_without_web_gets_mobile_only(self) -> None:
        # Mobile push on, web push off: the DM must reach the mobile device and
        # NOT fire web push, even though a subscription is registered.
        do_change_user_setting(self.user, "enable_web_push_notifications", False, acting_user=None)
        self.create_subscription()
        PushDeviceToken.objects.create(
            user=self.user, token="mobile-token", kind=PushDeviceToken.FCM
        )
        message_id = self.send_personal_message(self.sender, self.user, "hello")
        missed_message = {
            "message_id": message_id,
            "trigger": NotificationTriggers.DIRECT_MESSAGE,
        }
        with (
            override_settings(WEB_PUSH_ENABLED=True, WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            mock.patch(
                "zerver.lib.push_notifications.send_push_notifications_legacy"
            ) as mock_legacy,
            self.assertLogs("zerver.lib.push_notifications", level="INFO"),
        ):
            handle_push_notification(self.user.id, missed_message)
        mock_legacy.assert_called_once()
        mock_webpush.assert_not_called()


class WebPushDeviceRegisteredTest(ZulipTestCase):
    def test_web_push_subscription_counts_as_push_device(self) -> None:
        """A user whose only push registration is a web push subscription must
        still be marked push_device_registered by the message-send pipeline —
        that annotation gates every push notification trigger, so without it
        web push never fires for users without the mobile app."""
        hamlet = self.example_user("hamlet")
        othello = self.example_user("othello")
        WebPushSubscription.objects.create(
            user=othello,
            endpoint=EXAMPLE_ENDPOINT,
            p256dh_key=EXAMPLE_SUBSCRIPTION["p256dh_key"],
            auth_secret=EXAMPLE_SUBSCRIPTION["auth_secret"],
            user_agent=EXAMPLE_SUBSCRIPTION["user_agent"],
        )
        recipient = get_or_create_direct_message_group([hamlet.id, othello.id]).recipient
        assert recipient is not None
        info = get_recipient_info(
            realm_id=othello.realm_id,
            recipient=recipient,
            sender_id=hamlet.id,
            stream_topic=None,
        )
        self.assertIn(othello.id, info.push_device_registered_user_ids)
        self.assertNotIn(hamlet.id, info.push_device_registered_user_ids)

    def _dm_push_disabled_ids(self, sender_id: int, recipient_user: UserProfile) -> set[int]:
        recipient = get_or_create_direct_message_group([sender_id, recipient_user.id]).recipient
        assert recipient is not None
        info = get_recipient_info(
            realm_id=recipient_user.realm_id,
            recipient=recipient,
            sender_id=sender_id,
            stream_topic=None,
        )
        return info.dm_mention_push_disabled_user_ids

    def test_web_push_only_user_stays_push_eligible(self) -> None:
        # Mobile offline push off but web push on: the user must NOT be treated
        # as push-disabled for the DM/@-mention triggers, so the message still
        # reaches the push worker and web push can fire.
        hamlet = self.example_user("hamlet")
        othello = self.example_user("othello")
        do_change_user_setting(
            othello, "enable_offline_push_notifications", False, acting_user=None
        )
        do_change_user_setting(othello, "enable_web_push_notifications", True, acting_user=None)
        self.assertNotIn(othello.id, self._dm_push_disabled_ids(hamlet.id, othello))

    def test_user_with_both_push_channels_off_is_disabled(self) -> None:
        # Both mobile offline push and web push off: push-disabled as before.
        hamlet = self.example_user("hamlet")
        othello = self.example_user("othello")
        do_change_user_setting(
            othello, "enable_offline_push_notifications", False, acting_user=None
        )
        do_change_user_setting(othello, "enable_web_push_notifications", False, acting_user=None)
        self.assertIn(othello.id, self._dm_push_disabled_ids(hamlet.id, othello))

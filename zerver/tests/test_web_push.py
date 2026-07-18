import base64
from unittest import mock

import orjson
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import override_settings
from pywebpush import WebPushException

from zerver.actions.message_send import get_recipient_info
from zerver.actions.user_settings import do_change_user_setting
from zerver.lib.push_notifications import (
    handle_push_notification,
    has_webpush_credentials,
    push_notifications_configured,
    send_web_push_notifications,
)
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.web_push_vapid import derive_vapid_public_key
from zerver.models.push_notifications import WebPushSubscription
from zerver.models.scheduled_jobs import NotificationTriggers

EXAMPLE_ENDPOINT = "https://fcm.googleapis.com/fcm/send/example-endpoint"
EXAMPLE_SUBSCRIPTION = {
    "endpoint": EXAMPLE_ENDPOINT,
    "p256dh_key": "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkFb_zezF9x0",
    "auth_secret": "tBHItJI5svbpez7KI4CCXg",
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
        with override_settings(WEB_PUSH_VAPID_PRIVATE_KEY=None):
            self.assertFalse(has_webpush_credentials())
        with override_settings(WEB_PUSH_VAPID_PRIVATE_KEY=generate_vapid_private_key_b64()):
            self.assertTrue(has_webpush_credentials())

    def test_derive_vapid_public_key(self) -> None:
        public_key_b64 = derive_vapid_public_key(generate_vapid_private_key_b64())
        # The applicationServerKey is the unpadded base64url uncompressed
        # P-256 point: 65 bytes starting with the 0x04 uncompressed marker.
        padding = "=" * (-len(public_key_b64) % 4)
        point = base64.urlsafe_b64decode(public_key_b64 + padding)
        self.assertEqual(len(point), 65)
        self.assertEqual(point[0], 0x04)


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
        updated = {**EXAMPLE_SUBSCRIPTION, "p256dh_key": "BNewKeyValue0000000000000000000000"}
        result = self.api_post(user, self.ENDPOINT, updated)
        self.assert_json_success(result)

        self.assertEqual(WebPushSubscription.objects.filter(user=user).count(), 1)
        subscription = WebPushSubscription.objects.get(user=user)
        self.assertEqual(subscription.p256dh_key, "BNewKeyValue0000000000000000000000")

    def test_register_multiple_endpoints(self) -> None:
        user = self.example_user("hamlet")
        self.api_post(user, self.ENDPOINT, EXAMPLE_SUBSCRIPTION)
        second = {**EXAMPLE_SUBSCRIPTION, "endpoint": EXAMPLE_ENDPOINT + "-2"}
        self.api_post(user, self.ENDPOINT, second)

        self.assertEqual(WebPushSubscription.objects.filter(user=user).count(), 2)

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

    def test_unauthenticated_request_rejected(self) -> None:
        result = self.client_post(self.ENDPOINT, EXAMPLE_SUBSCRIPTION)
        self.assert_json_error(
            result, "Not logged in: API authentication or user session required", status_code=401
        )


class WebPushSendTest(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.vapid_key = generate_vapid_private_key_b64()
        self.user = self.example_user("hamlet")
        self.sender = self.example_user("iago")

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
            override_settings(WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            self.assertLogs("zerver.lib.push_notifications", level="INFO"),
        ):
            handle_push_notification(self.user.id, missed_message)
        return message_id, mock_webpush

    def test_send_web_push_notifications_calls_webpush(self) -> None:
        subscription = self.create_subscription()
        payload = {"type": "message", "message_id": 1}
        with (
            override_settings(WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
        ):
            send_web_push_notifications(self.user, payload, [subscription])

        mock_webpush.assert_called_once()
        args, kwargs = mock_webpush.call_args
        self.assertEqual(args[0]["endpoint"], subscription.endpoint)
        self.assertEqual(args[0]["keys"]["p256dh"], subscription.p256dh_key)
        self.assertEqual(args[0]["keys"]["auth"], subscription.auth_secret)
        self.assertEqual(orjson.loads(args[1])["type"], "message")
        self.assertTrue(kwargs["vapid_private_key"].startswith("-----BEGIN PRIVATE KEY-----"))
        self.assertTrue(kwargs["vapid_claims"]["sub"].startswith("mailto:"))

    def test_send_to_each_subscription(self) -> None:
        self.create_subscription(EXAMPLE_ENDPOINT)
        self.create_subscription(EXAMPLE_ENDPOINT + "-2")
        subscriptions = list(WebPushSubscription.objects.filter(user=self.user))
        with (
            override_settings(WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
        ):
            send_web_push_notifications(self.user, {"type": "message"}, subscriptions)
        self.assertEqual(mock_webpush.call_count, 2)

    def test_gone_subscription_is_pruned(self) -> None:
        subscription = self.create_subscription()
        with (
            override_settings(WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            self.assertLogs("zerver.lib.push_notifications", level="INFO"),
        ):
            mock_webpush.side_effect = WebPushException("Gone", response=mock.Mock(status_code=410))
            send_web_push_notifications(self.user, {"type": "message"}, [subscription])
        self.assertFalse(WebPushSubscription.objects.filter(id=subscription.id).exists())

    def test_other_error_keeps_subscription(self) -> None:
        subscription = self.create_subscription()
        with (
            override_settings(WEB_PUSH_VAPID_PRIVATE_KEY=self.vapid_key),
            mock.patch("pywebpush.webpush") as mock_webpush,
            self.assertLogs("zerver.lib.push_notifications", level="WARNING"),
        ):
            mock_webpush.side_effect = WebPushException(
                "Server error", response=mock.Mock(status_code=500)
            )
            # The failure must not escape the loop.
            send_web_push_notifications(self.user, {"type": "message"}, [subscription])
        self.assertTrue(WebPushSubscription.objects.filter(id=subscription.id).exists())

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
            override_settings(WEB_PUSH_VAPID_PRIVATE_KEY=None),
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
        recipient = othello.recipient
        assert recipient is not None
        info = get_recipient_info(
            realm_id=othello.realm_id,
            recipient=recipient,
            sender_id=hamlet.id,
            stream_topic=None,
        )
        self.assertIn(othello.id, info.push_device_registered_user_ids)
        self.assertNotIn(hamlet.id, info.push_device_registered_user_ids)

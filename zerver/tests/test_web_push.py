import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import override_settings

from zerver.lib.push_notifications import has_webpush_credentials
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.web_push_vapid import derive_vapid_public_key
from zerver.models.push_notifications import WebPushSubscription

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

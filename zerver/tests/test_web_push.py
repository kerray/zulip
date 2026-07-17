import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import override_settings

from zerver.lib.push_notifications import has_webpush_credentials
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.web_push_vapid import derive_vapid_public_key
from zerver.models.push_notifications import WebPushSubscription
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

    def test_non_public_endpoint_host_rejected(self) -> None:
        user = self.example_user("hamlet")
        # An endpoint we would POST to is client-supplied, so a host that
        # cannot be a public push service — loopback, RFC 1918, the cloud
        # metadata link-local address, an IPv4-mapped IPv6 loopback — must be
        # refused rather than stored and later fetched by the notification
        # worker.
        for bad_endpoint in (
            "https://localhost/push",
            "https://127.0.0.1/push",
            "https://10.0.0.5:8443/push",
            "https://192.168.1.1/push",
            "https://169.254.169.254/latest/meta-data/",
            "https://[::1]/push",
            "https://[::ffff:127.0.0.1]/push",
        ):
            payload = {**EXAMPLE_SUBSCRIPTION, "endpoint": bad_endpoint}
            result = self.api_post(user, self.ENDPOINT, payload)
            self.assert_json_error(result, "Invalid endpoint: Value error, Invalid push endpoint")
        self.assertEqual(WebPushSubscription.objects.count(), 0)

        # A public IP literal is not what real push services use, but it is not
        # our business to forbid it; only the non-routable ranges are refused.
        payload = {**EXAMPLE_SUBSCRIPTION, "endpoint": "https://93.184.216.34/push"}
        self.assert_json_success(self.api_post(user, self.ENDPOINT, payload))

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


# Web Push subscriptions and credential revocation.
class WebPushRevocationTest(ZulipTestCase):
    def create_subscription(self, user: UserProfile, endpoint: str) -> WebPushSubscription:
        return WebPushSubscription.objects.create(
            user=user,
            endpoint=endpoint,
            p256dh_key=EXAMPLE_P256DH_KEY,
            auth_secret=EXAMPLE_AUTH_SECRET,
        )

    def test_regenerate_api_key_deletes_web_push_subscriptions(self) -> None:
        # Regenerating a user's API key is Zulip's credential-revocation
        # hammer: `manage.py logout_all_users --rotate-api-keys` and the SAML
        # IdP-initiated logout handler both use it to cut a user off from every
        # client, and it already clears mobile push tokens and Device rows for
        # exactly that reason. Web Push subscriptions must die with them --
        # this server delivers those notifications itself, so a subscription
        # that outlives the rotation keeps pushing message content (sender and,
        # by default, body) to a browser the revocation was meant to cut off.
        from zerver.actions.user_settings import do_regenerate_api_key

        hamlet = self.example_user("hamlet")
        cordelia = self.example_user("cordelia")
        self.create_subscription(hamlet, f"{EXAMPLE_ENDPOINT}-hamlet-laptop")
        self.create_subscription(hamlet, f"{EXAMPLE_ENDPOINT}-hamlet-shared")
        cordelia_sub = self.create_subscription(cordelia, f"{EXAMPLE_ENDPOINT}-cordelia")

        do_regenerate_api_key(hamlet, hamlet)

        self.assertEqual(WebPushSubscription.objects.filter(user=hamlet).count(), 0)
        # Only the rotating user's subscriptions are revoked.
        self.assertTrue(WebPushSubscription.objects.filter(id=cordelia_sub.id).exists())

    def test_regenerate_api_key_endpoint_deletes_web_push_subscriptions(self) -> None:
        # The same revocation must hold over the endpoint a user hits when they
        # believe a credential of theirs has been compromised.
        hamlet = self.example_user("hamlet")
        self.create_subscription(hamlet, EXAMPLE_ENDPOINT)

        result = self.api_post(hamlet, "/api/v1/users/me/api_key/regenerate")
        self.assert_json_success(result)

        self.assertEqual(WebPushSubscription.objects.filter(user=hamlet).count(), 0)

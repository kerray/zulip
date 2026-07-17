import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import override_settings

from zerver.lib.push_notifications import has_webpush_credentials
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.web_push_vapid import derive_vapid_public_key


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

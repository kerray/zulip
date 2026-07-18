"""Helpers for Web Push (RFC 8291) VAPID key handling.

The server stores a single VAPID keypair as the ``web_push_vapid_private_key``
secret: a P-256 (secp256r1) private key serialized as PKCS#8 DER and
base64url-encoded (unpadded) so that it fits on one line in
``zulip-secrets.conf``. The public key that browsers pass to
``PushManager.subscribe`` as the ``applicationServerKey`` is derived from it at
runtime, so the two halves can never drift apart.
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey


def b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def load_vapid_private_key(private_key_b64: str) -> EllipticCurvePrivateKey:
    """Load the VAPID private key from its base64url PKCS#8 DER secret form."""
    private_key = serialization.load_der_private_key(b64url_decode(private_key_b64), password=None)
    assert isinstance(private_key, EllipticCurvePrivateKey)
    return private_key


def derive_vapid_public_key(private_key_b64: str) -> str:
    """Return the browser applicationServerKey for a VAPID private key.

    That is the base64url (unpadded) uncompressed EC point of the public half
    of the P-256 keypair stored in the ``web_push_vapid_private_key`` secret.
    """
    public_key = load_vapid_private_key(private_key_b64).public_key()
    point = public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64url_encode(point)

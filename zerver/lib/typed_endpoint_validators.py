import base64
import binascii
import ipaddress
import re
import zoneinfo
from collections.abc import Collection
from enum import Enum
from typing import TypeVar
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils.translation import gettext as _
from pydantic import AfterValidator, BeforeValidator, NonNegativeInt
from pydantic_core import PydanticCustomError

from zerver.lib.exceptions import JsonableError
from zerver.lib.timezone import canonicalize_timezone

# The Pydantic.StringConstraints does not have validation for the string to be
# of the specified length. So, we need to create a custom validator for that.


def check_string_fixed_length(string: str, length: int) -> str | None:
    if len(string) != length:
        raise PydanticCustomError(
            "string_fixed_length",
            "",
            {
                "length": length,
            },
        )
    return string


def check_string_in(val: str, possible_values: Collection[str]) -> str:
    if val not in possible_values:
        raise ValueError(_("Not in the list of possible values"))
    return val


def check_int_in(val: int, possible_values: Collection[int]) -> int:
    if val not in possible_values:
        raise ValueError(_("Not in the list of possible values"))
    return val


def check_int_in_validator(possible_values: Collection[int]) -> AfterValidator:
    return AfterValidator(lambda val: check_int_in(val, possible_values))


def check_string_in_validator(possible_values: Collection[str]) -> AfterValidator:
    return AfterValidator(lambda val: check_string_in(val, possible_values))


def check_url(val: str) -> str:
    validate = URLValidator()
    try:
        validate(val)
        return val
    except ValidationError:
        raise ValueError(_("Not a URL"))


def check_https_url(val: str) -> str:
    validate = URLValidator(schemes=["https"])
    try:
        validate(val)
        return val
    except ValidationError:
        raise ValueError(_("Not an https URL"))


def check_web_push_endpoint_url(val: str) -> str:
    # The endpoint is minted by the browser's push service, but it reaches us
    # from the client, so a malicious client can register whatever it likes and
    # aim our delivery POSTs at it. Reject the hosts that cannot possibly be a
    # public push service: an IP literal outside the globally routable ranges
    # (cloud metadata at 169.254.169.254, loopback, RFC 1918) or "localhost".
    # We deliberately do not resolve hostnames — that would put a DNS lookup in
    # a request path, and it is defeated by rebinding anyway; filtering egress
    # by resolved destination is the outgoing proxy's job (see OutgoingSession).
    check_https_url(val)
    host = urlsplit(val).hostname
    if host is None or host == "localhost":
        raise ValueError(_("Invalid push endpoint"))
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP literal, so it is a name we are not going to resolve.
        return val
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 and friends; is_global did not look through the
        # mapping on all the Python versions we support.
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise ValueError(_("Invalid push endpoint"))
    return val


_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")


def _decode_base64url(val: str) -> bytes:
    # Browser Web Push keys are unpadded base64url; restore the padding before
    # decoding so pywebpush (which base64url-decodes them at send time) never
    # chokes on values we accepted. urlsafe_b64decode silently drops bytes
    # outside the base64url alphabet, so reject those up front (and let a bad
    # length surface as the binascii.Error the decode raises) — we only want to
    # accept material that round-trips to exactly what a browser produced.
    if _BASE64URL_RE.fullmatch(val) is None:
        raise binascii.Error("Non-base64url character")
    return base64.urlsafe_b64decode(val + "=" * (-len(val) % 4))


def check_web_push_p256dh_key(val: str) -> str:
    # PushSubscription.getKey("p256dh") is the 65-byte uncompressed P-256
    # public point (leading 0x04 marker), base64url-encoded. Reject malformed
    # material at registration so it can't later raise deep inside pywebpush
    # and abort the notification worker's queue job.
    try:
        point = _decode_base64url(val)
    except (binascii.Error, ValueError):
        raise ValueError(_("Invalid p256dh key"))
    if len(point) != 65 or point[0] != 0x04:
        raise ValueError(_("Invalid p256dh key"))
    # A well-formed-looking blob can still be off the curve; verify it is an
    # actual P-256 point so pywebpush's ECDH cannot fail at send time.
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ValueError:
        raise ValueError(_("Invalid p256dh key"))
    return val


def check_web_push_auth_secret(val: str) -> str:
    # PushSubscription.getKey("auth") is 16 random bytes, base64url-encoded.
    try:
        secret = _decode_base64url(val)
    except (binascii.Error, ValueError):
        raise ValueError(_("Invalid auth secret"))
    if len(secret) != 16:
        raise ValueError(_("Invalid auth secret"))
    return val


def to_timezone_or_empty(s: str) -> str:
    try:
        s = canonicalize_timezone(s)
        zoneinfo.ZoneInfo(s)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return ""
    else:
        return s


def timezone_or_empty_validator() -> AfterValidator:
    return AfterValidator(to_timezone_or_empty)


def check_timezone(s: str) -> str:
    try:
        zoneinfo.ZoneInfo(canonicalize_timezone(s))
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        raise ValueError(_("Not a recognized time zone"))
    return s


def timezone_validator() -> AfterValidator:
    return AfterValidator(check_timezone)


def to_non_negative_int_or_none(s: str) -> NonNegativeInt | None:
    try:
        i = int(s)
        if i < 0:
            return None
        return i
    except ValueError:
        return None


# We use BeforeValidator, not AfterValidator, here, because the int
# type conversion will raise a ValueError if the string is not a valid
# integer, and we want to return None in that case.
def non_negative_int_or_none_validator() -> BeforeValidator:
    return BeforeValidator(to_non_negative_int_or_none)


def check_color(var_name: str, val: object) -> str:
    s = str(val)
    valid_color_pattern = re.compile(r"^#([a-fA-F0-9]{3,6})$")
    matched_results = valid_color_pattern.match(s)
    if not matched_results:
        raise ValueError(_("{var_name} is not a valid hex color code").format(var_name=var_name))
    return s


EnumT = TypeVar("EnumT", bound=Enum)


def parse_enum_from_string_value(
    val: str,
    setting_name: str,
    enum: type[EnumT],
) -> EnumT:
    try:
        return enum[val]
    except KeyError:
        raise JsonableError(_("Invalid {setting_name}").format(setting_name=setting_name))


def check_uint32(val: int) -> int:
    if not (0 <= val <= 4294967295):
        raise ValueError(_("Not a valid unsigned 32-bit integer"))
    return val


def uint32_validator() -> AfterValidator:
    return AfterValidator(check_uint32)

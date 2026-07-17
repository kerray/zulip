from django.http import HttpRequest, HttpResponse, JsonResponse
from django.templatetags.static import static

from zerver.lib.cache import cache_get, cache_set
from zerver.lib.subdomains import get_subdomain
from zerver.lib.thumbnail import BadImageError, resize_avatar
from zerver.lib.upload import get_realm_icon_image
from zerver.models import Realm
from zerver.models.realms import get_realm

# The PWA spec recommends a short_name of at most 12 characters, since that is
# roughly what fits under a home-screen icon.
MANIFEST_SHORT_NAME_MAX_LENGTH = 12

# Sizes we render the web app manifest icons at. Both are well above the
# ~192px installability threshold browsers expect for a PWA home-screen icon.
MANIFEST_ICON_SIZES = (192, 512)

# The manifest itself is cheap to regenerate and its contents (realm name and
# icon) can change, so cache it only briefly.
MANIFEST_CACHE_CONTROL = "public, max-age=3600"


def get_manifest_realm(request: HttpRequest) -> Realm | None:
    try:
        return get_realm(get_subdomain(request))
    except Realm.DoesNotExist:
        # The root domain and unknown subdomains still get an installable app
        # with stock Zulip branding.
        return None


def web_app_manifest(request: HttpRequest) -> HttpResponse:
    """A per-realm PWA web app manifest, keyed on the request's realm.

    Serving this dynamically (rather than as a static file) lets an installed
    app carry the realm's own name and uploaded icon; realms without an
    uploaded icon fall back to stock Zulip branding.
    """
    realm = get_manifest_realm(request)
    name = realm.name if realm is not None and realm.name else "Zulip"
    if realm is not None and realm.icon_source == Realm.ICON_UPLOADED:
        # Honest icon sizes: the realm icon URL points at a 100x100 image, too
        # small for installability, so we serve resized copies of the retained
        # lossless original from web_app_manifest_icon instead.
        icons = [
            {
                "src": f"/manifest/icon-{size}.png?version={realm.icon_version}",
                "sizes": f"{size}x{size}",
                "type": "image/png",
                "purpose": "any",
            }
            for size in MANIFEST_ICON_SIZES
        ]
    else:
        icons = [
            {
                "src": static(f"images/logo/zulip-icon-{size}x{size}.png"),
                "sizes": f"{size}x{size}",
                "type": "image/png",
                "purpose": "any",
            }
            for size in MANIFEST_ICON_SIZES
        ]

    manifest = {
        "name": name,
        "short_name": name[:MANIFEST_SHORT_NAME_MAX_LENGTH],
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": icons,
    }
    response = JsonResponse(manifest, content_type="application/manifest+json")
    response["Cache-Control"] = MANIFEST_CACHE_CONTROL
    return response


def web_app_manifest_icon(request: HttpRequest, size: int) -> HttpResponse:
    """A realm's uploaded icon, resized on the fly to a manifest icon size.

    The URL is `?version`-busted by the manifest, so the resized PNG can be
    cached hard.
    """
    if size not in MANIFEST_ICON_SIZES:
        return HttpResponse(status=404)

    realm = get_manifest_realm(request)
    if realm is None or realm.icon_source != Realm.ICON_UPLOADED:
        # No uploaded icon to resize; the manifest points such realms straight
        # at the stock static assets, but guard the route regardless.
        return HttpResponse(status=404)

    # Cache the resized PNG bytes. This route is unauthenticated, so without a
    # cache an attacker could force a fresh fetch-and-resize (CPU, plus an S3
    # round trip) on every request. The key is versioned on the icon, so an
    # icon change busts it; a manifest icon PNG is a few KB, far below
    # memcached's ~1MB value limit.
    cache_key = f"manifest_icon:{realm.id}:{realm.icon_version}:{size}"
    cached = cache_get(cache_key)
    if cached is not None:
        resized = cached[0]
    else:
        # The original can be missing or unreadable even when icon_source is
        # UPLOADED (e.g. a lost or half-migrated original): local storage
        # raises FileNotFoundError, the S3 backend raises ClientError. Treat
        # any of these, like a corrupt image, as a 404.
        from botocore.exceptions import ClientError

        try:
            resized = resize_avatar(get_realm_icon_image(realm), size)
        except (BadImageError, FileNotFoundError, ClientError):
            return HttpResponse(status=404)
        cache_set(cache_key, resized)

    response = HttpResponse(resized, content_type="image/png")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response

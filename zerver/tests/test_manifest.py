from unittest import mock

import orjson

from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import get_test_image_file
from zerver.lib.thumbnail import resize_avatar
from zerver.models import Realm
from zerver.models.realms import get_realm


class ManifestTest(ZulipTestCase):
    def upload_realm_icon(self) -> None:
        self.login("iago")
        with get_test_image_file("img.png") as fp:
            result = self.client_post("/json/realm/icon", {"file": fp})
        self.assert_json_success(result)
        self.logout()

    def test_manifest_requires_no_auth(self) -> None:
        result = self.client_get("/manifest.webmanifest")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result["Content-Type"], "application/manifest+json")
        self.assertEqual(result["Cache-Control"], "public, max-age=3600")

    def test_manifest_uses_realm_name(self) -> None:
        realm = get_realm("zulip")
        result = self.client_get("/manifest.webmanifest")
        manifest = orjson.loads(result.content)
        self.assertEqual(manifest["name"], realm.name)
        self.assertEqual(manifest["short_name"], realm.name[:12])
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")

    def test_manifest_short_name_is_truncated(self) -> None:
        realm = get_realm("zulip")
        realm.name = "A Very Long Organization Name"
        realm.save(update_fields=["name"])
        result = self.client_get("/manifest.webmanifest")
        manifest = orjson.loads(result.content)
        self.assertEqual(manifest["name"], "A Very Long Organization Name")
        self.assertEqual(manifest["short_name"], "A Very Long ")
        self.assertLessEqual(len(manifest["short_name"]), 12)

    def test_manifest_icons_without_uploaded_icon(self) -> None:
        realm = get_realm("zulip")
        self.assertNotEqual(realm.icon_source, Realm.ICON_UPLOADED)
        result = self.client_get("/manifest.webmanifest")
        manifest = orjson.loads(result.content)
        srcs = [icon["src"] for icon in manifest["icons"]]
        for src in srcs:
            self.assertIn("zulip-icon-", src)
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})

    def test_manifest_icons_with_uploaded_icon(self) -> None:
        self.upload_realm_icon()
        realm = get_realm("zulip")
        self.assertEqual(realm.icon_source, Realm.ICON_UPLOADED)
        result = self.client_get("/manifest.webmanifest")
        manifest = orjson.loads(result.content)
        srcs = [icon["src"] for icon in manifest["icons"]]
        self.assertIn(f"/manifest/icon-192.png?version={realm.icon_version}", srcs)
        self.assertIn(f"/manifest/icon-512.png?version={realm.icon_version}", srcs)

    def test_manifest_icon_serves_resized_png(self) -> None:
        self.upload_realm_icon()
        result = self.client_get("/manifest/icon-192.png")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result["Content-Type"], "image/png")

    def test_manifest_icon_is_cached(self) -> None:
        self.upload_realm_icon()
        with mock.patch("zerver.views.manifest.resize_avatar", wraps=resize_avatar) as mock_resize:
            first = self.client_get("/manifest/icon-192.png")
            second = self.client_get("/manifest/icon-192.png")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.content, second.content)
        # The second request is served from the cache, not resized again.
        mock_resize.assert_called_once()

    def test_manifest_icon_missing_original_returns_404(self) -> None:
        self.upload_realm_icon()
        with mock.patch(
            "zerver.views.manifest.get_realm_icon_image", side_effect=FileNotFoundError
        ):
            result = self.client_get("/manifest/icon-192.png")
        self.assertEqual(result.status_code, 404)

    def test_manifest_icon_rejects_unsupported_size(self) -> None:
        self.upload_realm_icon()
        result = self.client_get("/manifest/icon-999.png")
        self.assertEqual(result.status_code, 404)

    def test_manifest_icon_without_uploaded_icon(self) -> None:
        realm = get_realm("zulip")
        self.assertNotEqual(realm.icon_source, Realm.ICON_UPLOADED)
        result = self.client_get("/manifest/icon-192.png")
        self.assertEqual(result.status_code, 404)

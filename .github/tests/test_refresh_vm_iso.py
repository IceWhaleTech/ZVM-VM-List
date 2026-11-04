import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import refresh_vm_iso as refresh


def linux_image():
    return {
        "id": "linux-debian-13-7-0", "family": "Linux", "os": "Debian",
        "version": "13.7.0", "url": "https://iso.example.com/old.iso",
        "size": 100, "hash": "a" * 64, "releaseDate": 260101,
    }


def windows_image(lang="en-us"):
    return {
        "id": f"windows-11-24H2-{lang}", "family": "Windows", "os": "Windows 11",
        "arch": "amd64", "edition": "Multi-Edition", "langCode": lang,
        "version": "24H2", "build": "10.0.26100.1", "releaseDate": 241001,
        "url": f"https://iso.example.com/24H2-{lang}.iso", "size": 100, "hash": "a" * 64,
    }


def windows_candidate(image):
    candidate = deepcopy(image)
    candidate.update({
        "id": f"windows-11-25H2-{image['langCode']}", "version": "25H2",
        "build": "10.0.26200.1", "releaseDate": 250930, "size": 200,
        "url": f"https://iso.example.com/25H2-{image['langCode']}.iso", "hash": "B" * 64,
    })
    return candidate


class RefreshTests(unittest.TestCase):
    def run_windows(self, images, manifest, bad_urls=()):
        def probe(url):
            if url in bad_urls:
                return True, 199, "HEAD 200"
            return True, 200 if "25H2" in url else 100, "HEAD 200"

        with patch.object(refresh, "load_windows_manifest", return_value=manifest), \
                patch.object(refresh, "http_check_url", side_effect=probe):
            return refresh.refresh_images(images, check_latest=True)

    def test_repair_mode_does_not_resolve_healthy_image(self):
        images = [linux_image()]
        before = deepcopy(images)
        with patch.object(refresh, "load_windows_manifest", return_value=None), \
                patch.object(refresh, "http_check_url", return_value=(True, 100, "HEAD 200")), \
                patch.object(refresh, "resolve_image") as resolve:
            refresh.refresh_images(images)
        resolve.assert_not_called()
        self.assertEqual(images, before)

    def test_latest_mode_upgrades_healthy_linux_and_clears_old_hash(self):
        images = [linux_image()]
        with patch.object(refresh, "load_windows_manifest", return_value=None), \
                patch.object(refresh, "http_check_url", side_effect=[(True, 100, "ok"), (True, 200, "ok")]), \
                patch.object(refresh, "resolve_image", return_value={"url": "https://iso.example.com/new.iso", "version": "13.8.0"}):
            result = refresh.refresh_images(images, check_latest=True)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(images[0]["id"], "linux-debian-13-8-0")
        self.assertEqual(images[0]["size"], 200)
        self.assertEqual(images[0]["hash"], "")
        self.assertEqual(images[0]["releaseDate"], 260101)

    def test_unavailable_candidate_does_not_leak_metadata(self):
        images = [linux_image()]
        before = deepcopy(images)
        with patch.object(refresh, "load_windows_manifest", return_value=None), \
                patch.object(refresh, "http_check_url", return_value=(False, None, "HEAD 404")), \
                patch.object(refresh, "resolve_image", return_value={
                    "url": "https://iso.example.com/new.iso", "version": "13.8.0",
                    "size": 200, "hash": "b" * 64, "releaseDate": 260201,
                }):
            result = refresh.refresh_images(images)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(images, before)

    def test_windows_channel_survives_id_change_and_second_run_is_unchanged(self):
        images = [windows_image(), windows_image("zh-cn")]
        manifest = {refresh.windows_channel_key(image): windows_candidate(image) for image in images}
        result = self.run_windows(images, manifest)
        self.assertEqual(result["updated"], 2)
        self.assertTrue(all(image["version"] == "25H2" for image in images))
        self.assertTrue(all(image["hash"] == "b" * 64 for image in images))
        self.assertEqual(self.run_windows(images, manifest)["updated"], 0)

    def test_windows_missing_language_rolls_back_entire_product(self):
        images = [windows_image(), windows_image("zh-cn")]
        before = deepcopy(images)
        manifest = {refresh.windows_channel_key(images[0]): windows_candidate(images[0])}
        result = self.run_windows(images, manifest)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(images, before)
        self.assertTrue(any("language batch incomplete" in detail for detail in result["details"]))

    def test_windows_r2_size_mismatch_rolls_back_all_languages(self):
        images = [windows_image(), windows_image("zh-cn")]
        before = deepcopy(images)
        manifest = {refresh.windows_channel_key(image): windows_candidate(image) for image in images}
        result = self.run_windows(images, manifest, bad_urls=[manifest[refresh.windows_channel_key(images[1])]["url"]])
        self.assertEqual(result["updated"], 0)
        self.assertEqual(images, before)

    def test_windows_different_builds_are_not_published_together(self):
        images = [windows_image(), windows_image("zh-cn")]
        before = deepcopy(images)
        manifest = {refresh.windows_channel_key(image): windows_candidate(image) for image in images}
        manifest[refresh.windows_channel_key(images[1])]["build"] = "10.0.26200.2"
        result = self.run_windows(images, manifest)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(images, before)

    def test_windows_one_product_failure_does_not_block_other_product(self):
        images = [windows_image(), windows_image("zh-cn"), windows_image("fr-fr")]
        images[2].update({"id": "windows-10-22H2-fr", "os": "Windows 10", "version": "22H2"})
        manifest = {refresh.windows_channel_key(images[0]): windows_candidate(images[0])}
        win10 = deepcopy(images[2])
        win10.update({"url": "https://iso.example.com/updated-win10.iso", "hash": "b" * 64})
        manifest[refresh.windows_channel_key(images[2])] = win10
        result = self.run_windows(images, manifest)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(images[0]["version"], "24H2")
        self.assertEqual(images[2]["url"], win10["url"])

    def test_windows_invalid_metadata_is_rejected(self):
        image = windows_image()
        invalid_fields = [
            ("hash", ""), ("hash", "not-a-sha256"), ("size", 0), ("size", "200"),
            ("size", True), ("releaseDate", 251332), ("releaseDate", "250930"),
            ("build", "25H2"), ("url", "http://iso.example.com/new.iso"),
            ("langCode", "fr-fr"), ("arch", "arm64"), ("version", "23H2"),
            ("id", image["id"]),
        ]
        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                candidate = windows_candidate(image)
                candidate[field] = value
                with self.assertRaises(ValueError):
                    refresh.resolve_windows(image, {refresh.windows_channel_key(image): candidate})

    def test_windows_manifest_formats_remain_supported(self):
        image = windows_image()
        candidate = windows_candidate(image)
        for payload in (
            {image["id"]: candidate}, {"by_id": {image["id"]: candidate}},
            [image], {"by_channel": {refresh.windows_channel_key(image): candidate}},
        ):
            with self.subTest(payload=payload):
                manifest = refresh.parse_windows_manifest(payload)
                self.assertIsNotNone(refresh.resolve_windows(image, manifest))

    def test_malformed_manifest_is_rejected(self):
        for payload in ("bad", {"by_channel": []}, {"entry": "bad"}, [windows_image(), windows_image()]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                refresh.parse_windows_manifest(payload)

    def test_missing_manifest_does_not_rewrite_windows_metadata(self):
        images = [windows_image()]
        before = deepcopy(images)
        with patch.object(refresh, "load_windows_manifest", return_value=None), \
                patch.object(refresh, "http_check_url", return_value=(True, 123, "HEAD 200")):
            refresh.refresh_images(images, check_latest=True)
        self.assertEqual(images, before)

    def test_manifest_load_error_is_visible_in_report_data(self):
        with patch.object(refresh, "load_windows_manifest", side_effect=ValueError("invalid manifest")):
            result = refresh.refresh_images([])
        self.assertEqual(result["failed"], 1)
        self.assertIn("invalid manifest", result["details"][0])

    def test_range_probe_uses_full_size_and_closes_stream(self):
        response = MagicMock(status_code=206, headers={"Content-Length": "2", "Content-Range": "bytes 0-1/1234"})
        response.__enter__.return_value = response
        with patch.object(refresh.requests, "head", return_value=MagicMock(status_code=403)), \
                patch.object(refresh.requests, "get", return_value=response):
            self.assertEqual(refresh.http_check_url("https://iso.example.com/file.iso")[:2], (True, 1234))
        response.__exit__.assert_called_once()

    def test_html_challenge_is_not_accepted_as_an_iso(self):
        response = MagicMock(status_code=200, headers={"Content-Type": "text/html", "Content-Length": "1234"})
        with patch.object(refresh.requests, "head", return_value=response):
            self.assertFalse(refresh.http_check_url("https://iso.example.com/file.iso")[0])
        response.__enter__.return_value = response
        with patch.object(refresh.requests, "get", return_value=response):
            self.assertFalse(refresh._check_with_range_get("https://iso.example.com/file.iso")[0])

    def test_no_linux_release_candidates_is_reported_as_failure(self):
        with patch.object(refresh, "load_windows_manifest", return_value=None), \
                patch.object(refresh, "http_check_url", return_value=(True, 100, "HEAD 200")), \
                patch.object(refresh, "resolve_image", return_value=None):
            result = refresh.refresh_images([linux_image()], check_latest=True)
        self.assertEqual(result["failed"], 1)
        self.assertIn("no release candidates found", result["details"][-1])

    def test_arch_uses_versioned_filename_and_actual_release_date(self):
        with patch.object(refresh, "fetch_text", return_value='<a href="archlinux-2026.11.01-x86_64.iso">ISO</a>'):
            candidate = refresh.resolve_arch_linux({})
        self.assertEqual(candidate["version"], "26.11")
        self.assertEqual(candidate["releaseDate"], 261101)
        self.assertEqual(candidate["url"], "https://geo.mirror.pkgbuild.com/iso/2026.11.01/archlinux-2026.11.01-x86_64.iso")

    def test_fedora_supports_both_filename_layouts(self):
        for filename in ("Fedora-Workstation-Live-x86_64-45-1.4.iso", "Fedora-Workstation-Live-45-1.4.x86_64.iso"):
            with self.subTest(filename=filename), patch.object(refresh, "fetch_text", side_effect=[
                '<a href="45/">45</a>', f'<a href="{filename}">ISO</a>',
            ]):
                candidate = refresh.resolve_fedora({})
            self.assertEqual(candidate["version"], "45")
            self.assertTrue(candidate["url"].endswith(filename))

    def test_fedora_skips_unpublished_future_directory(self):
        filename = "Fedora-Workstation-Live-45-1.4.x86_64.iso"
        with patch.object(refresh, "fetch_text", side_effect=[
            '<a href="45/">45</a><a href="46/">46</a>',
            refresh.requests.HTTPError("not released"), f'<a href="{filename}">ISO</a>',
        ]):
            candidate = refresh.resolve_fedora({})
        self.assertEqual(candidate["version"], "45")

    def test_mint_selects_latest_stable_cinnamon_across_major_versions(self):
        with patch.object(refresh, "fetch_text", side_effect=[
            '<a href="21.2/">old</a><a href="22.3/">latest</a>',
            '<a href="linuxmint-22.3-cinnamon-64bit.iso">ISO</a>',
        ]):
            candidate = refresh.resolve_linux_mint({"version": "21.2"})
        self.assertEqual(candidate["version"], "22.3")
        self.assertTrue(candidate["url"].endswith("/22.3/linuxmint-22.3-cinnamon-64bit.iso"))

    def test_mint_skips_future_directory_without_stable_iso(self):
        with patch.object(refresh, "fetch_text", side_effect=[
            '<a href="22.3/">stable</a><a href="23/">future</a>',
            '<a href="linuxmint-23-beta-cinnamon-64bit.iso">beta</a>',
            '<a href="linuxmint-22.3-cinnamon-64bit.iso">ISO</a>',
        ]):
            candidate = refresh.resolve_linux_mint({})
        self.assertEqual(candidate["version"], "22.3")

    def test_ubuntu_tracks_latest_stable_desktop_including_non_lts(self):
        def product(kind, version, suffix=""):
            return {
                "arch": "amd64", "image_type": kind, "versions": {version: {"items": {"iso": {
                    "path": f"release/ubuntu-{version}{suffix}-desktop-amd64.iso",
                    "sha256": "b" * 64, "size": 200,
                }}}},
            }
        products = {
            "lts": product("desktop", "26.04.1"), "stable": product("desktop", "26.10"),
            "future": product("beta-desktop", "27.04", "-beta"), "daily": product("daily", "28.04"),
        }
        with patch.object(refresh, "fetch_text", return_value=json.dumps({"products": products})):
            candidate = refresh.resolve_ubuntu({"version": "24.04"})
        self.assertEqual(candidate["version"], "26.10")
        self.assertEqual(candidate["hash"], "b" * 64)

    def test_latest_mode_does_not_downgrade_linux_for_a_stale_mirror(self):
        images = [linux_image()]
        before = deepcopy(images)
        with patch.object(refresh, "load_windows_manifest", return_value=None), \
                patch.object(refresh, "http_check_url", return_value=(True, 100, "HEAD 200")), \
                patch.object(refresh, "resolve_image", return_value={"version": "12.0.0", "url": "https://iso.example.com/older.iso"}):
            result = refresh.refresh_images(images, check_latest=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(images, before)

    def test_cli_preserves_file_bytes_when_data_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "images.json"
            report = Path(directory) / "report.md"
            original = json.dumps([linux_image()], separators=(",", ":"))
            source.write_text(original, encoding="utf-8")
            with patch("sys.argv", ["refresh_vm_iso.py", "--file", str(source), "--report", str(report)]), \
                    patch.object(refresh, "load_windows_manifest", return_value=None), \
                    patch.object(refresh, "http_check_url", return_value=(True, 100, "HEAD 200")), \
                    patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(refresh.main(), 0)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertTrue(report.exists())


if __name__ == "__main__":
    unittest.main()

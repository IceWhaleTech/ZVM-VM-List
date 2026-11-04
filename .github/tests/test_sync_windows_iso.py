import hashlib
import base64
import importlib.util
import io
import json
import os
import re
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import refresh_vm_iso as refresh
import sync_windows_iso as sync
from test_refresh_vm_iso import windows_image


class S3Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeS3:
    def __init__(self):
        self.objects, self.events = {}, []

    def put(self, key, data, metadata=None):
        self.objects[key] = {
            "data": data, "Metadata": metadata or {},
            "ETag": '"' + hashlib.md5(data).hexdigest() + '"',
        }

    def get_object(self, Bucket, Key, Range=None, IfMatch=None):
        self.events.append(("get", Key))
        if Key not in self.objects:
            raise S3Error("NoSuchKey")
        obj = self.objects[Key]
        if IfMatch is not None and IfMatch != obj["ETag"]:
            raise S3Error("PreconditionFailed")
        if Range is not None:
            start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", Range).groups())
            data = obj["data"][start:end + 1]
            return {"Body": io.BytesIO(data), "ETag": obj["ETag"], "ContentLength": len(data),
                    "ContentRange": f"bytes {start}-{end}/{len(obj['data'])}"}
        return {"Body": io.BytesIO(obj["data"]), "ETag": obj["ETag"]}

    def head_object(self, Bucket, Key):
        self.events.append(("head", Key))
        if Key not in self.objects:
            raise S3Error("404")
        obj = self.objects[Key]
        return {"ContentLength": len(obj["data"]), "ETag": obj["ETag"], "Metadata": obj["Metadata"]}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.events.append(("upload", key))
        self.put(key, Path(filename).read_bytes(), ExtraArgs["Metadata"])

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.events.append(("publish", Key))
        current = self.objects.get(Key)
        if (kwargs.get("IfNoneMatch") == "*" and current) or (
            kwargs.get("IfMatch") and (not current or kwargs["IfMatch"] != current["ETag"])
        ):
            raise S3Error("PreconditionFailed")
        self.put(Key, Body)
        return {"ETag": self.objects[Key]["ETag"]}


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.images = [windows_image("en-us"), windows_image("fr-fr")]
        self.data = {image["langCode"]: ("ISO-" + image["langCode"]).encode() for image in self.images}
        self.source = sync.Source("26H2", 260929, {lang: hashlib.sha256(data).hexdigest() for lang, data in self.data.items()})
        self.client = FakeS3()
        self.store = sync.R2Store(self.client, "test-bucket")

    def run_batch(self, source=None, inspect=None, report=None, public_ok=True):
        source = source or self.source
        report = report if report is not None else {"images": []}

        def download(url, path, current_source, digest):
            data = next(data for lang, data in self.data.items() if self.source.hashes[lang] == digest)
            Path(path).write_bytes(data)
            return len(data)

        def probe(url):
            if ".microsoft.com/" in url:
                return True, 100, "HEAD 200"
            lang = url.split("/")[-2]
            self.client.events.append(("public", url))
            return public_ok, len(self.data[lang]), "HEAD 200"

        def resolve(images, *_):
            return {sync.windows_channel_key(image): "https://software.download.prss.microsoft.com/Win11_26H2_x64.iso" for image in images}

        with patch.object(sync, "resolve_downloads", side_effect=resolve), \
                patch.object(sync, "download_iso", side_effect=download) as downloader, \
                patch.object(sync, "inspect_iso", side_effect=inspect or (lambda *_: "10.0.26300.9457")), \
                patch.object(sync, "http_check_url", side_effect=probe), \
                patch("sys.stdout", new_callable=io.StringIO):
            sync.publish_batch(self.images, source, sync.DEFAULT_PUBLIC_BASE, self.store, report, "Fido.ps1", "powershell.exe")
        return report, downloader.call_count

    def test_complete_batch_publishes_last_and_consumer_can_apply_it(self):
        report, downloads = self.run_batch()
        self.assertEqual(downloads, 2)
        self.assertEqual(report["status"], "published")
        self.assertEqual(self.client.events[-1], ("publish", sync.DEFAULT_MANIFEST_KEY))
        manifest = refresh.parse_windows_manifest(json.loads(self.client.objects[sync.DEFAULT_MANIFEST_KEY]["data"]))
        images = deepcopy(self.images)
        images[0]["default_configuration"] = {"disk_size": 64000000000}
        with patch.object(refresh, "load_windows_manifest", return_value=manifest), \
                patch.object(refresh, "http_check_url", side_effect=lambda url: (
                    True, next((entry["size"] for entry in manifest.values() if entry["url"] == url), 100), "HEAD 200"
                )):
            result = refresh.refresh_images(images, check_latest=True)
        self.assertEqual(result["updated"], 2)
        self.assertTrue(all(image["version"] == "26H2" and image["url"].startswith(sync.DEFAULT_PUBLIC_BASE) for image in images))
        self.assertEqual(images[0]["default_configuration"]["disk_size"], 64000000000)

    def test_unchanged_batch_skips_iso_download_and_r2_body_reads(self):
        self.run_batch()
        self.client.events.clear()
        report, downloads = self.run_batch()
        self.assertEqual(report["status"], "unchanged")
        self.assertEqual(downloads, 0)
        self.assertEqual([event for event in self.client.events if event[0] == "get"], [("get", sync.DEFAULT_MANIFEST_KEY)])
        self.assertFalse(any(event[0] == "publish" for event in self.client.events))

    def test_missing_official_language_hash_fails_before_upload(self):
        source = sync.Source("26H2", 260929, {"en-us": self.source.hashes["en-us"]})
        with self.assertRaises(sync.SyncError):
            self.run_batch(source=source)
        self.assertEqual(self.client.events, [])

    def test_inconsistent_builds_do_not_publish_and_keep_staged_objects(self):
        report = {"images": []}
        with self.assertRaises(sync.SyncError):
            self.run_batch(inspect=lambda path, image, ps: "10.0.26300." + ("1" if image["langCode"] == "en-us" else "2"), report=report)
        self.assertNotIn(sync.DEFAULT_MANIFEST_KEY, self.client.objects)
        self.assertEqual(len([event for event in self.client.events if event[0] == "upload"]), 2)
        # Failed batches retain verified object uploads for investigation / retry.
        self.assertEqual(len(report["images"]), 2)

    def test_partial_upload_failure_preserves_manifest_and_retries_without_first_download(self):
        original = {"by_id": {"windows-10-old": {"url": "https://example.com/win10.iso"}}}
        self.client.put(sync.DEFAULT_MANIFEST_KEY, json.dumps(original).encode())
        before = self.client.objects[sync.DEFAULT_MANIFEST_KEY]["data"]
        upload = self.client.upload_file

        def fail_second(filename, bucket, key, ExtraArgs):
            if "/fr-fr/" in key:
                raise OSError("interrupted")
            upload(filename, bucket, key, ExtraArgs)

        with patch.object(self.client, "upload_file", side_effect=fail_second), self.assertRaises(sync.SyncError):
            self.run_batch()
        self.assertEqual(self.client.objects[sync.DEFAULT_MANIFEST_KEY]["data"], before)
        report, downloads = self.run_batch()
        self.assertEqual(downloads, 1)
        manifest = json.loads(self.client.objects[sync.DEFAULT_MANIFEST_KEY]["data"])
        self.assertEqual(manifest["by_id"], original["by_id"])
        self.assertEqual(report["status"], "published")

    def test_r2_corruption_is_detected_by_readback_before_publication(self):
        upload = self.client.upload_file

        def corrupt(filename, bucket, key, ExtraArgs):
            upload(filename, bucket, key, ExtraArgs)
            obj = self.client.objects[key]
            self.client.put(key, b"X" + obj["data"][1:], obj["Metadata"])

        with patch.object(self.client, "upload_file", side_effect=corrupt), self.assertRaises(sync.SyncError):
            self.run_batch()
        self.assertNotIn(sync.DEFAULT_MANIFEST_KEY, self.client.objects)

    def test_existing_object_with_unexpected_metadata_is_not_overwritten(self):
        key = sync.object_key(self.images[0], self.source)
        self.client.put(key, self.data["en-us"], {"sha256": "0" * 64})
        with self.assertRaises(sync.SyncError):
            self.run_batch()
        self.assertFalse(any(event[0] == "upload" for event in self.client.events))

    def test_manifest_etag_conflict_does_not_overwrite_other_publisher(self):
        self.client.put(sync.DEFAULT_MANIFEST_KEY, b'{"by_channel": {}}')
        _, etag = self.store.read_manifest()
        concurrent = b'{"by_channel": {}, "note": "changed concurrently"}'
        self.client.put(sync.DEFAULT_MANIFEST_KEY, concurrent)
        with self.assertRaises(sync.SyncError):
            self.store.publish({}, etag)
        self.assertEqual(self.client.objects[sync.DEFAULT_MANIFEST_KEY]["data"], concurrent)

    def test_access_denied_is_not_treated_as_missing_manifest(self):
        with patch.object(self.client, "get_object", side_effect=S3Error("AccessDenied")), self.assertRaises(sync.SyncError):
            self.store.read_manifest()

    def test_public_probe_failure_keeps_manifest_unpublished(self):
        with self.assertRaises(sync.SyncError):
            self.run_batch(public_ok=False)
        self.assertNotIn(sync.DEFAULT_MANIFEST_KEY, self.client.objects)
        self.assertTrue(any(event[0] == "public" for event in self.client.events))

    def test_failed_language_preflight_does_not_transfer_or_read_back_existing_isos(self):
        self.client.put(sync.object_key(self.images[0], self.source), self.data["en-us"])
        with patch.object(sync, "resolve_downloads", side_effect=sync.SyncError("HTTP 503")), \
                patch.object(sync, "download_iso") as download, patch.object(self.store, "verify_bytes") as verify, \
                patch("sys.stdout", new_callable=io.StringIO), self.assertRaisesRegex(sync.SyncError, "HTTP 503"):
            sync.publish_batch(self.images, self.source, sync.DEFAULT_PUBLIC_BASE, self.store, {}, "Fido.ps1", "powershell.exe")
        download.assert_not_called()
        verify.assert_not_called()
        self.assertFalse(any(event[0] in ("upload", "publish") for event in self.client.events))

    @unittest.skipUnless(importlib.util.find_spec("boto3"), "Windows publisher SDK is optional for Linux-only refresh")
    def test_manifest_conditions_match_real_boto3_api(self):
        import boto3
        from botocore.stub import ANY, Stubber

        client = boto3.client(
            "s3", endpoint_url="https://example.r2.cloudflarestorage.com", region_name="auto",
            aws_access_key_id="test", aws_secret_access_key="test",
        )
        store = sync.R2Store(client, "test-bucket")
        common = {
            "Bucket": "test-bucket", "Key": sync.DEFAULT_MANIFEST_KEY, "Body": ANY,
            "ContentType": "application/json; charset=utf-8",
            "CacheControl": "no-cache, max-age=0, must-revalidate",
        }
        with Stubber(client) as stub:
            stub.add_response("put_object", {"ETag": '"new"'}, {**common, "IfNoneMatch": "*"})
            stub.add_response("put_object", {"ETag": '"next"'}, {**common, "IfMatch": '"old"'})
            store.publish({}, None)
            store.publish({}, '"old"')
            stub.assert_no_pending_responses()
        data = b"verified ISO bytes"
        with Stubber(client) as stub:
            stub.add_response("get_object", {
                "Body": io.BytesIO(data), "ContentLength": len(data), "ETag": '"iso-etag"',
                "ContentRange": f"bytes 0-{len(data) - 1}/{len(data)}",
            }, {"Bucket": "test-bucket", "Key": "test.iso", "Range": f"bytes=0-{len(data) - 1}", "IfMatch": '"iso-etag"'})
            store.verify_bytes("test.iso", hashlib.sha256(data).hexdigest(), len(data), '"iso-etag"')
            stub.assert_no_pending_responses()


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.data = bytes(range(37))
        self.client = FakeS3()
        self.client.put("test.iso", self.data)
        self.etag = self.client.objects["test.iso"]["ETag"]
        self.store = sync.R2Store(self.client, "test-bucket")

    def verify(self):
        with patch.object(sync, "READBACK_PART_SIZE", 8), patch("sys.stdout", new_callable=io.StringIO):
            self.store.verify_bytes("test.iso", hashlib.sha256(self.data).hexdigest(), len(self.data), self.etag)

    def test_out_of_order_ranges_produce_the_original_file_hash(self):
        get = self.client.get_object
        second = threading.Event()

        def reordered(**kwargs):
            if kwargs["Range"] == "bytes=0-7":
                if not second.wait(2):
                    raise RuntimeError("Ranges were not fetched concurrently")
            else:
                second.set()
            return get(**kwargs)

        with patch.object(self.client, "get_object", side_effect=reordered) as request:
            self.verify()
        self.assertEqual({call.kwargs["Range"] for call in request.call_args_list},
                         {"bytes=0-7", "bytes=8-15", "bytes=16-23", "bytes=24-31", "bytes=32-36"})
        self.assertTrue(all(call.kwargs["IfMatch"] == self.etag for call in request.call_args_list))

    def test_interrupted_body_retries_only_its_range_and_closes_all_bodies(self):
        get, bodies = self.client.get_object, []
        interrupted = False

        class Interrupted(io.BytesIO):
            def read(self, *args):
                raise TimeoutError("signed URL must not reach logs")

        def flaky(**kwargs):
            nonlocal interrupted
            result = get(**kwargs)
            if kwargs["Range"] == "bytes=8-15" and not interrupted:
                interrupted = True
                result["Body"] = Interrupted()
            bodies.append(result["Body"])
            return result

        with patch.object(self.client, "get_object", side_effect=flaky) as request, patch.object(sync.time, "sleep"):
            self.verify()
        self.assertEqual(sum(call.kwargs["Range"] == "bytes=8-15" for call in request.call_args_list), 2)
        self.assertEqual(request.call_count, 6)
        self.assertTrue(all(body.closed for body in bodies))

    def test_changed_object_is_rejected_without_retry(self):
        self.client.put("test.iso", b"changed")
        with patch.object(sync.time, "sleep") as sleep, self.assertRaisesRegex(sync.SyncError, "PreconditionFailed"):
            self.verify()
        sleep.assert_not_called()

    def test_server_ignoring_range_is_rejected_and_body_closed(self):
        body = io.BytesIO(self.data)
        with patch.object(self.client, "get_object", return_value={"Body": body, "ETag": self.etag, "ContentLength": len(self.data)}), \
                self.assertRaisesRegex(sync.SyncError, "unexpected byte range"):
            self.verify()
        self.assertTrue(body.closed)

    def test_persistent_stream_failure_is_bounded_and_does_not_publish(self):
        with patch.object(self.client, "get_object", side_effect=TimeoutError("https://example.com/?secret=hidden")) as get, \
                patch.object(sync.time, "sleep"), self.assertRaises(sync.SyncError) as error:
            self.verify()
        self.assertLessEqual(get.call_count, 3 * sync.READBACK_WORKERS)
        self.assertIn("TimeoutError", str(error.exception))
        self.assertNotIn("hidden", str(error.exception))


class DiscoveryTests(unittest.TestCase):
    def fixture(self):
        rows = "".join(f"<tr><td>{label}</td><td>{'a' * 64}</td></tr>" for label, _ in sync.LANGUAGES.values())
        download = f"<p>(Current release: Windows 11 2026 Update | Version 26H2)</p><table>{rows}</table>"
        release = "<table><tr><td>26H2</td><td>General Availability Channel</td><td>2026-09-29</td></tr></table>"
        return download, release

    def test_extracts_version_feature_date_and_all_official_hashes(self):
        source = sync.parse_source(*self.fixture())
        self.assertEqual(source.version, "26H2")
        self.assertEqual(source.release_date, 260929)
        self.assertEqual(set(source.hashes), set(sync.LANGUAGES))

    def test_challenge_or_malformed_checksum_does_not_become_a_plan(self):
        download, release = self.fixture()
        for html in ("<html>Access denied</html>", download.replace("a" * 64, "invalid")):
            with self.subTest(html=html[:25]), self.assertRaises(sync.SyncError):
                sync.parse_source(html, release)

    def test_download_url_must_match_official_host_version_and_architecture(self):
        for url in (
            "https://example.com/Win11_26H2_x64.iso",
            "https://microsoft.com.example.com/Win11_26H2_x64.iso",
            "https://software.download.prss.microsoft.com/Win11_25H2_x64.iso",
            "https://software.download.prss.microsoft.com/Win11_26H2_arm64.iso",
        ):
            with self.subTest(url=url), self.assertRaises(sync.SyncError):
                sync.validate_microsoft_url(url, "26H2")

    def test_build_named_microsoft_iso_is_accepted_without_a_marketing_version(self):
        for name in ("Windows11_Client_x64_en-us_26300_9457.iso", "Win11_26H2_English_x64.iso"):
            url = "https://software.download.prss.microsoft.com/dbazure/" + name + "?signed=secret"
            with self.subTest(name=name):
                self.assertEqual(sync.validate_microsoft_url(url, "26H2"), url)
        for name in ("Windows11_Client_arm64_en-us_26300_9457.iso", "Windows10_Client_x64_en-us_26300_9457.iso",
                     "Windows11_Client_x64_en-us_26300_9457.exe", "unknown_x64.iso"):
            with self.subTest(name=name), self.assertRaises(sync.SyncError):
                sync.validate_microsoft_url("https://software.download.prss.microsoft.com/" + name, "26H2")

    def test_download_filename_error_omits_signed_query(self):
        url = "https://software.download.prss.microsoft.com/Win11_25H2_English_x64.iso?token=not-for-logs"
        with self.assertRaises(sync.SyncError) as error:
            sync.validate_microsoft_url(url, "26H2")
        self.assertIn("win11_25h2_english_x64.iso", str(error.exception))
        self.assertNotIn("token", str(error.exception))
        self.assertNotIn("not-for-logs", str(error.exception))

    def test_public_domain_is_separate_from_s3_endpoint(self):
        self.assertEqual(sync.validate_public_base("https://vm-iso.zimaos.com/"), sync.DEFAULT_PUBLIC_BASE)
        for base in ("http://vm-iso.zimaos.com", "https://account.r2.cloudflarestorage.com", "https://user:pass@iso.example.com"):
            with self.subTest(base=base), self.assertRaises(sync.SyncError):
                sync.validate_public_base(base)

    def test_powershell_child_does_not_receive_cloud_credentials(self):
        with patch.dict(os.environ, {"R2_SECRET_ACCESS_KEY": "test-secret", "AWS_ACCESS_KEY_ID": "test-key"}), \
                patch.object(sync.subprocess, "run", return_value=MagicMock(returncode=0, stdout="ok")) as run:
            self.assertEqual(sync.run_powershell("script.ps1", []), "ok")
        child_env = run.call_args.kwargs["env"]
        self.assertNotIn("R2_SECRET_ACCESS_KEY", child_env)
        self.assertNotIn("AWS_ACCESS_KEY_ID", child_env)

    def test_powershell_failure_keeps_useful_diagnostics_without_secrets_or_signed_urls(self):
        result = MagicMock(returncode=3, stdout=(
            "Error: HTTP 503 https://software.download.prss.microsoft.com/image.iso?token=signed-secret "
            "session ID 12345678-1234-1234-1234-123456789abc"), stderr="test-cloud-secret")
        with patch.dict(os.environ, {"R2_SECRET_ACCESS_KEY": "test-cloud-secret"}), \
                patch.object(sync.subprocess, "run", return_value=result), self.assertRaises(sync.SyncError) as error:
            sync.run_powershell("Fido.ps1", [])
        self.assertIn("HTTP 503", str(error.exception))
        for secret in ("signed-secret", "test-cloud-secret", "12345678-1234-1234-1234-123456789abc"):
            self.assertNotIn(secret, str(error.exception))

    def test_download_preflight_checks_all_languages_and_reuses_resolved_urls(self):
        images = [windows_image("en-us"), windows_image("fr-fr")]
        url = "https://software.download.prss.microsoft.com/Win11_26H2_x64.iso?token=secret"
        resolved = {sync.windows_channel_key(image): url for image in images}
        with patch.object(sync, "resolve_downloads", return_value=resolved) as resolve, \
                patch.object(sync, "http_check_url", return_value=(True, 9000, "HEAD 200")), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            urls = sync.check_downloads(images, sync.Source("26H2", 260929, {}), "Fido.ps1", "powershell.exe")
        self.assertEqual(set(urls), {sync.windows_channel_key(image) for image in images})
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(resolve.call_args.args[0], images)
        self.assertNotIn("token=secret", output.getvalue())

    def test_batch_resolver_uses_one_powershell_session_and_validates_every_returned_url(self):
        images = [windows_image("en-us"), windows_image("zh-cn")]
        url = "https://software.download.prss.microsoft.com/Windows11_Client_x64_en-us_26300_9457.iso?token=secret"
        source = sync.Source("26H2", 260929, {})
        with tempfile.TemporaryDirectory() as directory:
            fido = Path(directory) / "Fido.ps1"
            fido.touch()
            for output in ({"en-us": url, "zh-cn": url}, {"en-us": url}, {"en-us": url, "zh-cn": "https://example.com/image.iso"}):
                with self.subTest(languages=list(output)), patch.object(sync, "run_powershell", return_value=json.dumps(output)) as run:
                    if len(output) == 2 and "example.com" not in output["zh-cn"]:
                        result = sync.resolve_downloads(images, source, fido, "powershell.exe")
                        self.assertEqual(set(result), {sync.windows_channel_key(image) for image in images})
                        self.assertEqual(run.call_count, 1)
                        args = run.call_args.args[1]
                        payload = json.loads(base64.b64decode(args[args.index("-RequestBase64") + 1]))
                        self.assertEqual([entry["code"] for entry in payload["languages"]], ["en-us", "zh-cn"])
                    else:
                        with self.assertRaises(sync.SyncError):
                            sync.resolve_downloads(images, source, fido, "powershell.exe")

    def test_missing_configuration_names_only_the_missing_setting(self):
        with patch.dict(os.environ, {
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "test-access-key",
            "R2_SECRET_ACCESS_KEY": "test-secret-not-for-logs",
        }, clear=True), self.assertRaises(sync.SyncError) as error:
            sync.create_store(sync.DEFAULT_MANIFEST_KEY)
        self.assertEqual(str(error.exception), "Missing R2 configuration: R2_BUCKET_NAME")
        self.assertNotIn("test-secret-not-for-logs", str(error.exception))

    def test_iso_inspection_rejects_wrong_arch_language_and_single_edition(self):
        good = {"build": "10.0.26300.9457", "architecture": 9, "languages": ["en-US"], "image_count": 10}
        for field, value in (("architecture", 12), ("languages", ["fr-FR"]), ("image_count", 1), ("build", "26H2")):
            with self.subTest(field=field), patch.object(sync, "run_powershell", return_value=json.dumps({**good, field: value})), \
                    self.assertRaises(sync.SyncError):
                sync.inspect_iso("test.iso", windows_image(), "powershell.exe")

    def test_iso_build_includes_dism_service_pack_revision(self):
        info = {"build": "10.0.26300", "revision": 9457, "architecture": 9, "languages": ["en-US"], "image_count": 10}
        with patch.object(sync, "run_powershell", return_value=json.dumps(info)):
            self.assertEqual(sync.inspect_iso("test.iso", windows_image(), "powershell.exe"), "10.0.26300.9457")
        info.pop("revision")
        with patch.object(sync, "run_powershell", return_value=json.dumps(info)), self.assertRaises(sync.SyncError):
            sync.inspect_iso("test.iso", windows_image(), "powershell.exe")

    def test_download_hash_and_size_are_checked(self):
        data = b"verified ISO bytes"
        digest = hashlib.sha256(data).hexdigest()
        url = "https://software.download.prss.microsoft.com/dbazure/Windows11_Client_x64_en-us_26300_9457.iso"
        source = sync.Source("26H2", 260929, {"en-us": digest})
        with tempfile.TemporaryDirectory() as directory:
            for length, expected_hash, valid in ((len(data), digest, True), (len(data) + 1, digest, False), (len(data), "0" * 64, False)):
                response = MagicMock(status_code=200, url=url, headers={"Content-Length": str(length)})
                response.__enter__.return_value = response
                response.iter_content.return_value = [data[:4], b"", data[4:]]
                with self.subTest(length=length, valid=valid), patch.object(sync.requests, "get", return_value=response):
                    path = Path(directory) / "test.iso"
                    if valid:
                        self.assertEqual(sync.download_iso(url, path, source, expected_hash), len(data))
                    else:
                        with self.assertRaises(sync.SyncError):
                            sync.download_iso(url, path, source, expected_hash)

    def test_preview_needs_no_credentials_fido_or_r2_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.json"
            report = Path(directory) / "report.json"
            catalog.write_text(json.dumps([windows_image()]))
            with patch("sys.argv", ["sync_windows_iso.py", "--dry-run", "--file", str(catalog), "--report", str(report)]), \
                    patch.object(sync, "discover_source", return_value=sync.Source("26H2", 260929, {"en-us": "a" * 64})), \
                    patch.object(sync, "create_store") as store, patch.object(sync, "resolve_download") as resolver, \
                    patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(sync.main(), 0)
            self.assertEqual(json.loads(report.read_text())["status"], "preview")
            store.assert_not_called()
            resolver.assert_not_called()

    def test_preview_can_check_real_download_boundary_without_r2_or_iso_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.json"
            report = Path(directory) / "report.json"
            catalog.write_text(json.dumps([windows_image()]))
            with patch("sys.argv", ["sync_windows_iso.py", "--dry-run", "--check-downloads", "--file", str(catalog), "--report", str(report)]), \
                    patch.object(sync, "discover_source", return_value=sync.Source("26H2", 260929, {"en-us": "a" * 64})), \
                    patch.object(sync, "create_store") as store, patch.object(sync, "download_iso") as download, \
                    patch.object(sync, "resolve_downloads", return_value={sync.windows_channel_key(windows_image()): "https://software.download.prss.microsoft.com/Win11_26H2_x64.iso?token=secret"}), \
                    patch.object(sync, "http_check_url", return_value=(True, 9000, "HEAD 200")), \
                    patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(sync.main(), 0)
            content = report.read_text()
            self.assertTrue(json.loads(content)["images"][0]["sourceChecked"])
            self.assertNotIn("token=secret", content)
            store.assert_not_called()
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()

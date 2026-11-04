#!/usr/bin/env python3
"""Discover official Windows 11 ISOs and publish verified language batches to R2."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import requests

from refresh_vm_iso import (
    HEADERS, http_check_url, parse_tuple_version, parse_windows_manifest,
    resolve_windows, windows_channel_key,
)

DOWNLOAD_PAGE = "https://www.microsoft.com/en-us/software-download/windows11"
RELEASE_PAGE = "https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information"
DEFAULT_PUBLIC_BASE = "https://vm-iso.zimaos.com"
DEFAULT_MANIFEST_KEY = "manifests/windows.json"
FIDO_COMMIT = "266084e3765ab8bf96767bd55c51b0ee70934551"
LANGUAGES = {
    "en-us": ("English 64-bit", "^English$"),
    "zh-cn": ("Chinese Simplified 64-bit", "^Chinese.*Simplified.*$"),
    "fr-fr": ("French 64-bit", "^French$"),
    "es-es": ("Spanish 64-bit", "^Spanish$"),
    "ja-jp": ("Japanese 64-bit", "^Japanese$"),
    "ko-kr": ("Korean 64-bit", "^Korean$"),
}
CHUNK_SIZE = 8 * 1024 * 1024


class SyncError(RuntimeError):
    pass


class Tables(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.rows, self.text = [], []
        self.row = self.cell = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        self.text.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = self.cell = None


@dataclass(frozen=True)
class Source:
    version: str
    release_date: int
    hashes: dict


def parse_source(download_html, release_html):
    download = Tables(download_html)
    match = re.search(
        r"Current release:\s*Windows 11[^)]*?\bVersion\s+(\d{2}H[12])",
        " ".join(download.text), re.IGNORECASE,
    )
    if not match:
        raise SyncError("Cannot determine the current release from the Microsoft download page")
    version = match[1].upper()
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
    labels = {normalize(label): lang for lang, (label, _) in LANGUAGES.items()}
    hashes = {}
    for row in download.rows:
        if len(row) < 2 or normalize(row[0]) not in labels:
            continue
        digest = re.sub(r"\s+", "", row[1]).lower()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise SyncError(f"Invalid official SHA256 for {row[0]}")
        lang = labels[normalize(row[0])]
        if lang in hashes and hashes[lang] != digest:
            raise SyncError(f"Conflicting official SHA256 values for {lang}")
        hashes[lang] = digest
    for row in Tables(release_html).rows:
        if len(row) >= 3 and row[0].strip().upper() == version and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row[2]):
            date = int(datetime.strptime(row[2], "%Y-%m-%d").strftime("%y%m%d"))
            return Source(version, date, hashes)
    raise SyncError(f"Cannot find the official feature release date for {version}")


def discover_source():
    pages = []
    for url in (DOWNLOAD_PAGE, RELEASE_PAGE):
        try:
            with requests.get(url, headers={**HEADERS, "Cache-Control": "no-cache"}, timeout=30) as response:
                response.raise_for_status()
                pages.append(response.text)
        except requests.RequestException as exc:
            raise SyncError(f"Microsoft metadata request failed: {type(exc).__name__}") from exc
    return parse_source(*pages)


def select_images(catalog, source):
    images = [image for image in catalog if image.get("family") == "Windows" and image.get("os") == "Windows 11"]
    if not images:
        raise SyncError("No Windows 11 entries found in the catalog")
    channels = set()
    for image in images:
        if image.get("arch") != "amd64" or image.get("edition") != "Multi-Edition":
            raise SyncError("The publisher currently supports Windows 11 amd64 Multi-Edition only")
        lang = image.get("langCode", "").lower()
        if lang not in LANGUAGES or lang not in source.hashes:
            raise SyncError(f"No official SHA256 available for {lang}")
        if parse_tuple_version(source.version) < parse_tuple_version(str(image.get("version", ""))):
            raise SyncError("Microsoft source version is older than the catalog")
        key = windows_channel_key(image)
        if key in channels:
            raise SyncError(f"Duplicate catalog channel: {key}")
        channels.add(key)
    return images


def validate_public_base(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SyncError("R2_PUBLIC_BASE_URL must be a public HTTPS base URL without credentials or query parameters")
    if parsed.hostname.endswith(".r2.cloudflarestorage.com"):
        raise SyncError("R2_PUBLIC_BASE_URL must be the public download domain, not the S3 API endpoint")
    return value.rstrip("/")


def validate_microsoft_url(url, version):
    parsed = urlsplit(url)
    name = unquote(parsed.path).rsplit("/", 1)[-1].lower()
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".microsoft.com")
            or parsed.username or parsed.password):
        raise SyncError("Download URL is not an official Microsoft HTTPS URL")
    # Microsoft also names ISOs by OS build, without the marketing release (26H2).
    # The release-specific official SHA256 remains the authority for their contents.
    build_name = re.fullmatch(r"windows11_client_x64_[a-z]{2}-[a-z]{2}_\d+_\d+\.iso", name)
    release_name = version.lower() in name and name.endswith(".iso") and "x64" in name
    if not (release_name or build_name):
        safe_name = re.sub(r"[^a-z0-9._-]", "_", name)[:180]
        raise SyncError(f"Unexpected Microsoft ISO filename for {version}/x64: {safe_name}")
    return url


def run_powershell(script, args, executable="powershell.exe"):
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-File", str(Path(script).resolve()), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
            env={key: value for key, value in os.environ.items()
                 if not key.startswith(("R2_", "AWS_")) and key not in ("GH_TOKEN", "GITHUB_TOKEN")},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SyncError(f"PowerShell execution failed: {type(exc).__name__}") from exc
    if result.returncode:
        detail = "\n".join((result.stdout or "", result.stderr or ""))
        for key, value in os.environ.items():
            if (key.startswith(("R2_", "AWS_")) or key in ("GH_TOKEN", "GITHUB_TOKEN")) and len(value) >= 4:
                detail = detail.replace(value, "[redacted]")
        detail = re.sub(r"https?://[^\s<>\"']+", "[URL redacted]", detail)
        detail = re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "[session redacted]", detail)
        detail = re.sub(r"\s+", " ", detail).strip()[-1200:]
        raise SyncError(f"{Path(script).name} failed with exit code {result.returncode}: {detail or 'No diagnostic output'}")
    return result.stdout.strip().lstrip("\ufeff")


def resolve_download(image, source, fido_script, powershell):
    if not Path(fido_script).is_file():
        raise SyncError(f"Fido script missing; check out pbatard/Fido at {FIDO_COMMIT}")
    output = run_powershell(fido_script, [
        "-Win", "11", "-Rel", source.version, "-Ed", "Home/Pro/Edu",
        "-Lang", LANGUAGES[image["langCode"].lower()][1],
        "-Arch", "^x64$", "-PlatformArch", "x64", "-GetUrl",
    ], powershell)
    urls = re.findall(r"https://[^\s<>\"']+", output)
    if len(urls) != 1:
        raise SyncError("Fido did not return exactly one download URL")
    return validate_microsoft_url(urls[0], source.version)


def check_downloads(images, source, fido_script, powershell):
    urls = {}
    for image in images:
        lang = image["langCode"]
        print(f"[preflight] {source.version} {lang}: resolving Microsoft download", flush=True)
        try:
            url = resolve_download(image, source, fido_script, powershell)
            ok, size, _ = http_check_url(url)
            if not ok or not size or size <= 0:
                raise SyncError("Microsoft ISO download link is unavailable or has no valid byte count")
        except SyncError as exc:
            raise SyncError(f"Microsoft download preflight failed for {lang}: {exc}") from exc
        print(f"[preflight] {lang}: {unquote(urlsplit(url).path).rsplit('/', 1)[-1]} ({size} bytes)", flush=True)
        urls[windows_channel_key(image)] = url
    return urls


def download_iso(url, path, source, expected_hash):
    validate_microsoft_url(url, source.version)
    digest, count = hashlib.sha256(), 0
    try:
        with requests.get(url, stream=True, timeout=(20, 120), headers={"Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            validate_microsoft_url(response.url, source.version)
            if response.status_code != 200 or "text/html" in response.headers.get("Content-Type", "").lower():
                raise SyncError("Microsoft did not return a complete ISO download")
            expected_size = response.headers.get("Content-Length")
            with Path(path).open("wb") as output:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        output.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
            if not count or (expected_size and count != int(expected_size)):
                raise SyncError("Microsoft ISO download is empty or incomplete")
    except requests.RequestException as exc:
        raise SyncError(f"Microsoft ISO download failed: {type(exc).__name__}") from exc
    if digest.hexdigest() != expected_hash:
        raise SyncError("Downloaded ISO SHA256 does not match Microsoft's published value")
    return count


def inspect_iso(path, image, powershell):
    script = Path(__file__).with_name("inspect_windows_iso.ps1")
    info = json.loads(run_powershell(script, ["-ISOPath", str(Path(path).resolve())], powershell))
    languages = info.get("languages", [])
    if isinstance(languages, str):
        languages = [languages]
    if str(info.get("architecture", "")).lower() not in ("9", "x64", "amd64"):
        raise SyncError("ISO architecture does not match amd64")
    if image["langCode"].lower() not in [language.lower() for language in languages]:
        raise SyncError("ISO language does not match the requested catalog language")
    build = str(info.get("build", ""))
    if re.fullmatch(r"10\.0\.\d+", build):
        revision = info.get("revision")
        if type(revision) is not int or revision < 0:
            raise SyncError("DISM did not provide the ISO build revision")
        build += f".{revision}"
    if info.get("image_count", 0) < 2 or not re.fullmatch(r"10\.0\.\d+\.\d+", build):
        raise SyncError("ISO is not the expected Windows multi-edition image or has no valid OS build")
    return build


def object_key(image, source):
    lang = image["langCode"].lower()
    return f"windows/11/amd64/multi-edition/{source.version}/{lang}/{source.hashes[lang]}.iso"


def candidate_entry(image, source, public_base, head):
    lang = image["langCode"].lower()
    old_marker = f"-{image['version']}-"
    new_id = image["id"].replace(old_marker, f"-{source.version}-", 1) if old_marker in image["id"] else f"windows-11-{source.version}-{lang}"
    entry = {field: image[field] for field in ("os", "arch", "edition", "langCode")}
    entry.update({
        "id": new_id, "url": public_base + "/" + quote(object_key(image, source), safe="/"),
        "version": source.version, "build": head.get("Metadata", {}).get("build", ""),
        "releaseDate": source.release_date, "size": head["ContentLength"],
        "hash": source.hashes[lang], "r2Etag": head.get("ETag", ""),
    })
    resolve_windows(image, {windows_channel_key(image): entry})
    return entry


def missing_object(exc):
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") in ("NoSuchKey", "NotFound", "404")


class R2Store:
    def __init__(self, client, bucket, manifest_key=DEFAULT_MANIFEST_KEY):
        self.client, self.bucket, self.manifest_key = client, bucket, manifest_key

    def read_manifest(self):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.manifest_key)
        except Exception as exc:
            if missing_object(exc):
                return {}, None
            raise SyncError("Unable to read R2 manifest; refusing to replace unknown state") from exc
        body = response["Body"]
        try:
            raw = body.read(2 * 1024 * 1024 + 1)
        finally:
            body.close()
        if len(raw) > 2 * 1024 * 1024 or not response.get("ETag"):
            raise SyncError("R2 manifest is too large or has no ETag")
        return parse_windows_manifest(json.loads(raw)), response["ETag"]

    def head(self, key):
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if missing_object(exc):
                return None
            raise SyncError("Unable to inspect R2 ISO object") from exc

    def upload(self, path, key, metadata):
        try:
            self.client.upload_file(str(path), self.bucket, key, ExtraArgs={
                "Metadata": metadata, "ContentType": "application/octet-stream",
                "CacheControl": "public, max-age=31536000, immutable",
            })
        except Exception as exc:
            raise SyncError("R2 multipart ISO upload failed") from exc

    def verify_bytes(self, key, expected_hash, expected_size):
        digest, count = hashlib.sha256(), 0
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                while chunk := body.read(CHUNK_SIZE):
                    digest.update(chunk)
                    count += len(chunk)
            finally:
                body.close()
        except Exception as exc:
            raise SyncError("Unable to read back the R2 ISO for verification") from exc
        if count != expected_size or digest.hexdigest() != expected_hash:
            raise SyncError("R2 read-back SHA256 or size verification failed")

    def publish(self, entries, etag):
        payload = {"by_channel": {key: value for key, value in entries.items() if "/" in key}}
        legacy = {key: value for key, value in entries.items() if "/" not in key}
        if legacy:
            payload["by_id"] = legacy
        condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=self.manifest_key,
                Body=(json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                ContentType="application/json; charset=utf-8",
                CacheControl="no-cache, max-age=0, must-revalidate", **condition,
            )
        except Exception as exc:
            raise SyncError("R2 manifest publication failed or another publisher changed its ETag; rerun to merge fresh state") from exc


def create_store(manifest_key):
    names = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
    missing = [name for name in names if not os.getenv(name, "").strip()]
    if missing:
        raise SyncError("Missing R2 configuration: " + ", ".join(missing))
    account = os.environ["R2_ACCOUNT_ID"].strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
        raise SyncError("R2_ACCOUNT_ID must be your Cloudflare account ID, not the public r2.dev hostname")
    import boto3
    from botocore.config import Config
    client = boto3.client(
        "s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com", region_name="auto",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            request_checksum_calculation="when_required", response_checksum_validation="when_required",
        ),
    )
    return R2Store(client, os.environ["R2_BUCKET_NAME"], manifest_key)


def stage_image(image, source, public_base, store, previous, fido_script, powershell, work_dir, download_url=None):
    key = object_key(image, source)
    digest = source.hashes[image["langCode"].lower()]
    head = store.head(key)
    state = "reused"
    if head is None:
        with tempfile.TemporaryDirectory(prefix="windows-iso-", dir=work_dir) as directory:
            path = Path(directory) / "image.iso"
            print(f"[download] {source.version} {image['langCode']}", flush=True)
            url = download_url or resolve_download(image, source, fido_script, powershell)
            print(f"[source] {unquote(urlsplit(url).path).rsplit('/', 1)[-1]}", flush=True)
            size = download_iso(url, path, source, digest)
            print(f"[verified] {image['langCode']}: Microsoft SHA256 matched ({size} bytes)", flush=True)
            build = inspect_iso(path, image, powershell)
            print(f"[upload] {image['langCode']}: Windows build {build}", flush=True)
            store.upload(path, key, {
                "sha256": digest, "build": build, "version": source.version,
                "language": image["langCode"].lower(), "architecture": "amd64",
            })
            head = store.head(key)
            if head is None or head.get("ContentLength") != size:
                raise SyncError("Uploaded R2 object size does not match the verified local ISO")
        state = "uploaded"
    meta = head.get("Metadata", {})
    if (
        meta.get("sha256") != digest or meta.get("version") != source.version
        or meta.get("language") != image["langCode"].lower() or meta.get("architecture") != "amd64"
        or not head.get("ETag") or head.get("ContentLength", 0) <= 0
    ):
        raise SyncError("Existing content-addressed R2 object has unexpected metadata; refusing to overwrite it")
    entry = candidate_entry(image, source, public_base, head)
    already_verified = (
        previous.get("hash") == digest and previous.get("r2Etag") == entry["r2Etag"]
        and all(previous.get(field) == entry[field] for field in ("size", "build", "version", "url"))
    )
    if not already_verified:
        print(f"[readback] {image['langCode']}: verifying R2 object SHA256", flush=True)
        store.verify_bytes(key, digest, entry["size"])
    ok, size, _ = http_check_url(entry["url"])
    if not ok or size != entry["size"]:
        raise SyncError("Public R2 download URL is unavailable or has an incorrect byte count")
    print(f"[ready] {image['langCode']}: R2 object and public download verified", flush=True)
    return entry, state


def publish_batch(catalog, source, public_base, store, report, fido_script, powershell, work_dir=None):
    images = select_images(catalog, source)
    existing, etag = store.read_manifest()
    # Resolve every missing language before downloading or reading back large ISOs.
    missing = [image for image in images if store.head(object_key(image, source)) is None]
    downloads = check_downloads(missing, source, fido_script, powershell)
    merged = dict(existing)
    staged = []
    for image in images:
        channel = windows_channel_key(image)
        previous = existing.get(channel, existing.get(image["id"], {}))
        if previous and parse_tuple_version(str(previous.get("version", ""))) > parse_tuple_version(source.version):
            raise SyncError("Microsoft source is older than the published R2 manifest")
        entry, state = stage_image(image, source, public_base, store, previous, fido_script, powershell, work_dir,
                                   downloads.get(channel))
        if (
            previous.get("version") == entry["version"] and previous.get("build")
            and parse_tuple_version(entry["build"])[2:] < parse_tuple_version(previous["build"])[2:]
        ):
            raise SyncError("ISO build is older than the published build")
        staged.append(entry)
        merged[channel] = entry
        report["images"].append({"channel": channel, "state": state, **entry})
    if len({entry["build"] for entry in staged}) != 1:
        raise SyncError("Windows language builds differ; the manifest was not published")
    selected_ids = {image["id"] for image in images}
    ids = [image["id"] for image in catalog if image["id"] not in selected_ids] + [entry["id"] for entry in staged]
    if len(ids) != len(set(ids)):
        raise SyncError("New Windows ids conflict with the catalog")
    if merged != existing:
        store.publish(merged, etag)
        report["status"] = "published"
    else:
        report["status"] = "unchanged"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="vm-iso.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--publish", action="store_true", help="Download, verify, and publish a complete Windows 11 batch")
    mode.add_argument("--dry-run", action="store_true", help="Inspect metadata and show a plan without ISO downloads or R2 access (default)")
    parser.add_argument("--check-downloads", action="store_true", help="Also resolve and probe every Microsoft download link in preview mode")
    parser.add_argument("--public-base-url", default=os.getenv("R2_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE))
    parser.add_argument("--manifest-key", default=os.getenv("R2_MANIFEST_KEY", DEFAULT_MANIFEST_KEY))
    parser.add_argument("--fido-script", default=".tools/fido/Fido.ps1")
    parser.add_argument("--powershell", default="powershell.exe")
    parser.add_argument("--work-dir", help="Temporary ISO directory; only one ISO is retained at a time")
    parser.add_argument("--report", help="Write a JSON report, including failures")
    args = parser.parse_args()
    report = {"status": "failed", "version": None, "images": [], "error": None}
    try:
        public_base = validate_public_base(args.public_base_url)
        if args.manifest_key.startswith("/") or ".." in args.manifest_key.split("/") or not args.manifest_key.endswith(".json"):
            raise SyncError("Manifest key must be a relative JSON object key")
        catalog = json.loads(Path(args.file).read_text(encoding="utf-8"))
        source = discover_source()
        images = select_images(catalog, source)
        report["version"], report["releaseDate"] = source.version, source.release_date
        report["manifest_url"] = public_base + "/" + quote(args.manifest_key, safe="/")
        if args.publish:
            if os.name != "nt":
                raise SyncError("Publication requires Windows for Fido and DISM; use --dry-run on macOS/Linux")
            publish_batch(catalog, source, public_base, create_store(args.manifest_key), report,
                          args.fido_script, args.powershell, args.work_dir)
        else:
            downloads = check_downloads(images, source, args.fido_script, args.powershell) if args.check_downloads else {}
            report["status"] = "preview"
            report["images"] = [{
                "channel": windows_channel_key(image), "hash": source.hashes[image["langCode"].lower()],
                "url": public_base + "/" + quote(object_key(image, source), safe="/"),
            } for image in images]
            if args.check_downloads:
                for entry in report["images"]:
                    entry["sourceFilename"] = unquote(urlsplit(downloads[entry["channel"]]).path).rsplit("/", 1)[-1]
                    entry["sourceChecked"] = True
    except Exception as exc:
        report["status"], report["error"] = "failed", str(exc)
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    print(text)
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
import argparse
import json
import os
import re
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

TIMEOUT = 20
HEADERS = {
    "User-Agent": "ZVM-VM-List/1.0 (+https://github.com/IceWhaleTech/ZVM-VM-List)",
}


def http_check_url(url: str) -> Tuple[bool, Optional[int], str]:
    try:
        resp = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            if "text/html" in resp.headers.get("Content-Type", "").lower():
                return False, None, "HEAD returned HTML instead of an ISO"
            size = resp.headers.get("Content-Length")
            return True, int(size) if size and size.isdigit() else None, "HEAD 200"

        if resp.status_code in (403, 405):
            return _check_with_range_get(url)

        return False, None, f"HEAD {resp.status_code}"
    except requests.RequestException as exc:
        if "timed out" in str(exc).lower():
            return _check_with_range_get(url)
        return False, None, f"HEAD error: {exc}"


def _check_with_range_get(url: str) -> Tuple[bool, Optional[int], str]:
    headers = dict(HEADERS)
    headers["Range"] = "bytes=0-1"
    try:
        with requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, stream=True) as resp:
            if resp.status_code in (200, 206):
                if "text/html" in resp.headers.get("Content-Type", "").lower():
                    return False, None, "GET range returned HTML instead of an ISO"
                size = resp.headers.get("Content-Length")
                if resp.status_code == 206:
                    crange = resp.headers.get("Content-Range", "")
                    m = re.search(r"/(\d+)$", crange)
                    if m:
                        return True, int(m.group(1)), "GET range 206"
                return True, int(size) if size and size.isdigit() else None, f"GET range {resp.status_code}"
            return False, None, f"GET range {resp.status_code}"
    except requests.RequestException as exc:
        return False, None, f"GET range error: {exc}"


def fetch_text(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def parse_tuple_version(version_text: str) -> Tuple[int, ...]:
    parts = [int(p) for p in re.findall(r"\d+", version_text)]
    return tuple(parts) if parts else (0,)


def pick_latest(items: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    if not items:
        return None
    return sorted(items, key=lambda x: parse_tuple_version(x[1]))[-1]


def resolve_fedora(image: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Use the fixed official host for directory listings; the download redirector
    # can select a mirror that forbids browsing even when an ISO is reachable.
    base = "https://dl.fedoraproject.org/pub/fedora/linux/releases/"
    page = fetch_text(base)
    major_versions = re.findall(r'href="(\d{2})/"', page)
    if not major_versions:
        return None

    # Fedora may publish future version directories before the ISO is available.
    # Scan versions from newest to oldest and pick the first one with a valid ISO.
    for major in sorted(set(major_versions), key=lambda x: int(x), reverse=True):
        iso_dir = f"{base}{major}/Workstation/x86_64/iso/"
        try:
            iso_page = fetch_text(iso_dir)
        except requests.RequestException:
            continue

        candidates = []
        for pattern in (
            rf'(Fedora-Workstation-Live-x86_64-({major}(?:-\d+\.\d+)?)\.iso)',
            rf'(Fedora-Workstation-Live-({major}(?:-\d+\.\d+)?)\.x86_64\.iso)',
        ):
            candidates.extend(re.findall(pattern, iso_page))

        if candidates:
            latest = pick_latest([(name, build) for name, build in candidates])
            if latest:
                filename, _ = latest
                return {"url": iso_dir + filename, "version": major}

        fallback = f"{iso_dir}Fedora-Workstation-Live-x86_64-{major}-1.4.iso"
        ok, _, _ = http_check_url(fallback)
        if ok:
            return {"url": fallback, "version": major}

    return None


def resolve_debian(image: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    iso_dir = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/"
    page = fetch_text(iso_dir)
    candidates = re.findall(r'(debian-(\d+\.\d+\.\d+)-amd64-netinst\.iso)', page)
    latest = pick_latest([(name, version) for name, version in candidates])
    if not latest:
        return None
    filename, version = latest
    return {"url": iso_dir + filename, "version": version}


def resolve_linux_mint(image: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    root = "https://mirrors.edge.kernel.org/linuxmint/stable/"
    page = fetch_text(root)
    versions = re.findall(r'href="(\d+(?:\.\d+)?)/"', page)
    if not versions:
        return None

    for version in sorted(set(versions), key=parse_tuple_version, reverse=True):
        iso_dir = f"{root}{version}/"
        try:
            iso_page = fetch_text(iso_dir)
        except requests.RequestException:
            continue
        candidates = re.findall(
            rf'(linuxmint-({re.escape(version)})-cinnamon-64bit\.iso)',
            iso_page,
        )
        if candidates:
            return {"url": iso_dir + candidates[0][0], "version": version}
    return None


def resolve_arch_linux(image: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    iso_root = "https://geo.mirror.pkgbuild.com/iso/"
    page = fetch_text(iso_root + "latest/")
    candidates = re.findall(r'(archlinux-(\d{4}\.\d{2}\.\d{2})-x86_64\.iso)', page)
    latest = pick_latest(candidates)
    if not latest:
        return None
    filename, latest_date = latest
    return {
        "url": f"{iso_root}{latest_date}/{filename}",
        "version": f"{latest_date[2:4]}.{latest_date[5:7]}",
        "releaseDate": int(latest_date[2:].replace(".", "")),
    }


def resolve_ubuntu(image: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # The ISO feed tracks published images. The upgrade-offer feed can lag
    # behind new ISO availability. Select desktop explicitly to exclude beta,
    # daily, server, and WSL products, and allow stable non-LTS upgrades.
    root = "https://releases.ubuntu.com/"
    payload = json.loads(fetch_text(root + "streams/v1/com.ubuntu.releases:ubuntu.json"))
    candidates = []
    for product in payload.get("products", {}).values():
        if product.get("arch") != "amd64" or product.get("image_type") != "desktop":
            continue
        for release in product.get("versions", {}).values():
            iso = release.get("items", {}).get("iso", {})
            path = iso.get("path", "")
            match = re.fullmatch(r"(?:[a-zA-Z0-9._-]+/)*ubuntu-(\d+\.\d+(?:\.\d+)?)-desktop-amd64\.iso", path)
            if not match or not re.fullmatch(r"[a-fA-F0-9]{64}", str(iso.get("sha256", ""))):
                continue
            if type(iso.get("size")) is not int or iso["size"] <= 0:
                continue
            candidates.append({
                "url": root + path, "version": match[1],
                "hash": iso["sha256"].lower(), "size": iso["size"],
            })
    return max(candidates, key=lambda item: parse_tuple_version(item["version"])) if candidates else None


def parse_windows_manifest(payload: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(payload, dict):
        if "by_channel" in payload or "by_id" in payload:
            result = {}
            for field in ("by_id", "by_channel"):
                entries = payload.get(field, {})
                if not isinstance(entries, dict):
                    raise ValueError(f"Windows manifest {field} must be an object")
                if result.keys() & entries.keys():
                    raise ValueError("Windows manifest contains duplicate keys")
                result.update(entries)
        else:
            result = payload
    elif isinstance(payload, list):
        result = {}
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ValueError("Windows manifest array entries must have an id")
            if item["id"] in result:
                raise ValueError("Windows manifest contains duplicate ids")
            result[item["id"]] = item
    else:
        raise ValueError("Windows manifest must be an object or array")

    if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in result.items()):
        raise ValueError("Windows manifest must map string keys to image objects")
    return result


def load_windows_manifest() -> Optional[Dict[str, Dict[str, Any]]]:
    manifest_url = os.getenv("WINDOWS_ISO_MANIFEST_URL", "").strip()
    if not manifest_url:
        return None

    resp = requests.get(manifest_url, headers={**HEADERS, "Cache-Control": "no-cache"}, timeout=TIMEOUT)
    resp.raise_for_status()
    return parse_windows_manifest(resp.json())


def windows_channel_key(image: Dict[str, Any]) -> str:
    # Unlike the public id, this key survives feature and build updates.
    fields = ("os", "arch", "edition", "langCode")
    return "/".join(re.sub(r"[^a-z0-9]+", "-", str(image.get(field, "")).lower()).strip("-") for field in fields)


def resolve_windows(image: Dict[str, Any], windows_manifest: Optional[Dict[str, Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    if not windows_manifest:
        return None
    candidate = windows_manifest.get(windows_channel_key(image))
    if candidate is None:
        candidate = windows_manifest.get(image.get("id", ""))
    if candidate is None:
        return None

    for field in ("url", "version", "build", "hash"):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            raise ValueError(f"Windows manifest entry requires a non-empty {field}")
    url = urlsplit(candidate["url"])
    if url.scheme != "https" or not url.netloc or not url.path.lower().endswith(".iso"):
        raise ValueError("Windows manifest URL must be an HTTPS ISO URL")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", candidate["hash"]):
        raise ValueError("Windows manifest hash must be a SHA256 digest")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", candidate["build"]):
        raise ValueError("Windows manifest build must be an OS build such as 10.0.26100.1")
    if type(candidate.get("size")) is not int or candidate["size"] <= 0:
        raise ValueError("Windows manifest size must be a positive byte count")
    if type(candidate.get("releaseDate")) is not int or len(str(candidate["releaseDate"])) != 6:
        raise ValueError("Windows manifest releaseDate must be a YYMMDD integer")
    datetime.strptime(str(candidate["releaseDate"]), "%y%m%d")
    for field in ("os", "arch", "edition", "langCode"):
        if field in candidate and str(candidate[field]).casefold() != str(image.get(field, "")).casefold():
            raise ValueError(f"Windows manifest {field} does not match the selected image")

    if parse_tuple_version(candidate["version"]) < parse_tuple_version(str(image.get("version", ""))):
        raise ValueError("Windows manifest version is older than the current version")
    if "id" in candidate and (not isinstance(candidate["id"], str) or not candidate["id"].strip()):
        raise ValueError("Windows manifest id must be a non-empty string")
    if candidate["version"] != image.get("version") and candidate.get("id", image.get("id")) == image.get("id"):
        raise ValueError("Windows version change requires a new id; use by_channel for stable matching")

    resolved = {
        k: candidate[k]
        for k in ("id", "url", "version", "build", "releaseDate", "hash", "size")
        if k in candidate
    }
    resolved["hash"] = resolved["hash"].lower()
    return resolved


def resolve_image(image: Dict[str, Any], windows_manifest: Optional[Dict[str, Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    os_name = image.get("os", "")
    family = image.get("family", "")

    if family == "Windows":
        return resolve_windows(image, windows_manifest)
    if os_name == "Fedora":
        return resolve_fedora(image)
    if os_name == "Debian":
        return resolve_debian(image)
    if os_name == "Linux Mint":
        return resolve_linux_mint(image)
    if os_name == "Arch Linux":
        return resolve_arch_linux(image)
    if os_name == "Ubuntu":
        return resolve_ubuntu(image)
    return None


def expected_id(image: Dict[str, Any]) -> Optional[str]:
    if image.get("family") != "Linux":
        return None

    os_name = image.get("os", "")
    version = str(image.get("version", "")).strip()
    if not version:
        return None

    if os_name == "Debian":
        return f"linux-debian-{version.replace('.', '-')}"
    if os_name == "Arch Linux":
        return f"linux-archlinux-{version}"
    if os_name == "Fedora":
        major = version.split(".")[0]
        return f"linux-fedora-{major}"
    if os_name == "Linux Mint":
        return f"linux-linuxmint-{version}-cinnamon"
    if os_name == "Ubuntu":
        # Keep Ubuntu id on release branch (e.g. 24.04), not patch version.
        branch = version
        m = re.match(r"^(\d+\.\d+)", version)
        if m:
            branch = m.group(1)
        return f"linux-ubuntu-{branch}"

    return None


def sync_metadata(image: Dict[str, Any]) -> bool:
    changed = False
    eid = expected_id(image)
    if eid and image.get("id") != eid:
        image["id"] = eid
        changed = True
    return changed


def refresh_images(images: List[Dict[str, Any]], check_latest: bool = False) -> Dict[str, Any]:
    summary = {
        "checked": 0,
        "available": 0,
        "updated": 0,
        "metadata_updated": 0,
        "failed": 0,
        "unresolved": 0,
        "details": [],
    }
    windows_manifest = None
    try:
        windows_manifest = load_windows_manifest()
    except Exception as exc:  # noqa: BLE001
        summary["failed"] += 1
        summary["details"].append(f"[error] Failed to load Windows manifest: {exc}")

    windows_original = {index: deepcopy(image) for index, image in enumerate(images) if image.get("family") == "Windows"}
    validated_windows = set()

    for image_index, image in enumerate(images):
        image_id = image.get("id", "<unknown>")
        original = image.copy()
        summary["checked"] += 1

        ok, size, reason = http_check_url(image["url"])
        if ok:
            summary["available"] += 1
            if size and image.get("size") != size:
                if image.get("family") == "Windows":
                    summary["details"].append(f"[warn] {image_id}: R2 size differs from catalog; awaiting validated manifest")
                else:
                    image["size"] = size
                    image["hash"] = ""
                    summary["metadata_updated"] += 1
                    summary["details"].append(f"[metadata] {image_id}: size synchronized")
            if sync_metadata(image):
                summary["metadata_updated"] += 1
                summary["details"].append(f"[metadata] {image_id}: id synchronized to {image['id']}")
            summary["details"].append(f"[ok] {image_id}: {reason}")
            if not check_latest:
                continue

        else:
            summary["details"].append(f"[broken] {image_id}: {reason} -> {image['url']}")
        old_url = image["url"]

        try:
            resolved = resolve_image(image, windows_manifest)
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            summary["details"].append(f"[error] {image_id}: resolver error: {exc}")
            continue

        if not resolved:
            if ok:
                if image.get("family") == "Windows":
                    summary["details"].append(f"[skip] {image_id}: no Windows manifest entry configured")
                else:
                    summary["failed"] += 1
                    summary["details"].append(f"[error] {image_id}: no release candidates found; check source format or access")
                continue
            summary["unresolved"] += 1
            if image.get("family") == "Windows":
                summary["details"].append(
                    f"[todo] {image_id}: no Windows manifest entry. Configure WINDOWS_ISO_MANIFEST_URL to enable auto-replace"
                )
            else:
                summary["details"].append(f"[todo] {image_id}: no replacement candidate found")
            continue

        candidate = image.copy()
        candidate.update(resolved)
        if (
            image.get("family") == "Linux" and check_latest and resolved.get("version")
            and parse_tuple_version(str(resolved["version"])) < parse_tuple_version(str(image.get("version", "")))
        ):
            summary["failed"] += 1
            summary["details"].append(f"[error] {image_id}: source candidate is older than the catalog; kept current entry")
            continue

        if ok and candidate["url"] == old_url:
            ok2, new_size, reason2 = ok, size, reason
        else:
            ok2, new_size, reason2 = http_check_url(candidate["url"])
        if not ok2:
            summary["failed"] += 1
            summary["details"].append(f"[error] {image_id}: resolved URL still unavailable ({reason2}), candidate discarded")
            continue

        if image.get("family") == "Windows" and new_size != resolved["size"]:
            summary["failed"] += 1
            summary["details"].append(f"[error] {image_id}: R2 size does not match manifest, candidate discarded")
            continue

        if new_size:
            candidate["size"] = new_size
        elif candidate["url"] != old_url and "size" not in resolved:
            summary["failed"] += 1
            summary["details"].append(f"[error] {image_id}: replacement size unknown, candidate discarded")
            continue

        artifact_changed = any(candidate.get(key) != original.get(key) for key in ("url", "version", "build", "size"))
        if artifact_changed and "hash" not in resolved:
            candidate["hash"] = ""

        if sync_metadata(candidate):
            summary["metadata_updated"] += 1
            summary["details"].append(f"[metadata] {image_id}: id synchronized to {candidate['id']}")

        if any(other is not image and other.get("id") == candidate.get("id") for other in images):
            summary["failed"] += 1
            summary["details"].append(f"[error] {image_id}: replacement id already exists, candidate discarded")
            continue

        if image.get("family") == "Windows":
            validated_windows.add(image_index)
        if candidate != image:
            image.update(candidate)
            summary["updated"] += 1
            summary["details"].append(f"[updated] {image_id}: {old_url} -> {image['url']} (version {image.get('version', '')})")
        else:
            summary["details"].append(f"[latest] {image_id}: already current")

    # Publish all existing languages of a Windows product together. A partially
    # uploaded batch or a failed probe must not leave the catalog half upgraded.
    windows_groups: Dict[Tuple[str, ...], List[int]] = {}
    for index, original in windows_original.items():
        group = tuple(str(original.get(field, "")) for field in ("os", "arch", "edition"))
        windows_groups.setdefault(group, []).append(index)
    for group, indices in windows_groups.items():
        changed = [index for index in indices if images[index] != windows_original[index]]
        if not changed:
            continue
        releases = {(images[index].get("version"), images[index].get("build")) for index in indices}
        if not all(index in validated_windows for index in indices) or len(releases) != 1:
            for index in indices:
                images[index].clear()
                images[index].update(windows_original[index])
            summary["updated"] -= len(changed)
            summary["failed"] += 1
            summary["details"].append(
                f"[error] {' / '.join(group)}: language batch incomplete or builds differ; rolled back {len(changed)} updates"
            )

    return summary


def write_report(report_path: str, summary: Dict[str, Any]) -> None:
    lines = [
        "# VM ISO Refresh Report",
        "",
        f"- Checked: {summary['checked']}",
        f"- Available: {summary['available']}",
        f"- Updated: {summary['updated']}",
        f"- Metadata updated: {summary['metadata_updated']}",
        f"- Failed: {summary['failed']}",
        f"- Unresolved: {summary['unresolved']}",
        "",
        "## Details",
    ]
    lines.extend(f"- {item}" for item in summary["details"])

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh image URLs and metadata in vm-iso.json")
    parser.add_argument("--file", default="vm-iso.json", help="Path to vm-iso.json")
    parser.add_argument("--report", default="refresh-report.md", help="Path to markdown report")
    parser.add_argument("--check-latest", action="store_true", help="Resolve latest versions even when existing URLs are reachable")
    parser.add_argument("--strict", action="store_true", help="Fail when any refresh errors or unresolved images remain")
    args = parser.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        images = json.load(f)

    original = deepcopy(images)
    summary = refresh_images(images, check_latest=args.check_latest)

    if images != original:
        with open(args.file, "w", encoding="utf-8") as f:
            json.dump(images, f, indent=4, ensure_ascii=False)
            f.write("\n")

    write_report(args.report, summary)

    print(json.dumps({k: v for k, v in summary.items() if k != "details"}, ensure_ascii=False))
    for line in summary["details"]:
        print(line)

    return 1 if args.strict and (summary["failed"] or summary["unresolved"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())

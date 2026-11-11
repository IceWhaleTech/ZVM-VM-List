import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

def check_url(image):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }

    try:
        headers["Range"] = "bytes=0-511"
        with requests.get(image["url"], headers=headers, timeout=10, allow_redirects=True, stream=True) as response:
            if response.status_code not in (200, 206):
                return f"[unavailable] {image['id']}: HTTP {response.status_code}"

            # Bot challenges can return HTTP 200. Check the actual response body,
            # including when a server ignores Range and sends the full image.
            prefix = next(response.iter_content(chunk_size=512), b"")
            if ("text/html" in response.headers.get("Content-Type", "").lower()
                    or prefix.lstrip().lower().startswith((b"<!doctype html", b"<html"))):
                return f"[unavailable] {image['id']}: HTML returned instead of an ISO (possible bot challenge)"
            if not prefix:
                return f"[unavailable] {image['id']}: empty download response"

            if response.status_code == 206:
                match = re.fullmatch(r"bytes 0-\d+/(\d+)", response.headers.get("Content-Range", ""))
                if not match:
                    return f"[unavailable] {image['id']}: invalid Content-Range"
                size = int(match.group(1))
            else:
                length = response.headers.get("Content-Length", "")
                size = int(length) if length.isdigit() else None
            if size is not None and size != image["size"]:
                return f"[unavailable] {image['id']}: size mismatch ({size} != {image['size']})"
            return None

    except requests.RequestException as exc:
        return f"[unavailable] {image['id']}: {exc}"


def main():
    with open("vm-iso.json", "r", encoding="utf-8") as f:
        image_list = json.load(f)

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {executor.submit(check_url, image): image for image in image_list}

        for future in as_completed(future_to_url):
            result = future.result()
            if result:
                print(result)


if __name__ == "__main__":
    main()

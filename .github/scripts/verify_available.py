import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

def check_url(image):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }

    try:
        response = requests.head(image["url"], headers=headers, timeout=10, allow_redirects=True)
        if response.status_code in (200, 206):
            return None

        if response.status_code in (403, 405):
            range_headers = dict(headers)
            range_headers["Range"] = "bytes=0-1"
            with requests.get(image["url"], headers=range_headers, timeout=10, allow_redirects=True, stream=True) as fallback:
                if fallback.status_code in (200, 206):
                    return None

        if response.status_code == 429:
            return None

        return f"[unavailable] {image['id']}: HTTP {response.status_code}"

    except requests.RequestException as exc:
        return f"[unavailable] {image['id']}: {exc}"


with open("vm-iso.json", "r", encoding="utf-8") as f:
    image_list = json.load(f)

with ThreadPoolExecutor(max_workers=10) as executor:
    future_to_url = {executor.submit(check_url, image): image for image in image_list}

    for future in as_completed(future_to_url):
        result = future.result()
        if result:
            print(result)

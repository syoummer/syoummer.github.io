#!/usr/bin/env python3
"""Download celebrity portraits for people.html into assets/people."""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
PEOPLE_HTML = ROOT / "people.html"
ASSET_DIR = ROOT / "assets" / "people"
CATALOG_CSV = ASSET_DIR / "catalog.csv"
MANIFEST_JSON = ASSET_DIR / "manifest.json"
FALLBACK_LIST = Path("/Users/syou/Downloads/list.txt")
MANUAL_WIKI_OVERRIDES = {
    "李在玟": {"lang": "en", "title": "Lee_Jeno"},
}
BAIKE_PREFERRED = {
    "周柯宇",
    "姚明明",
    "张耀",
    "李马克",
    "王皓轩",
    "赖冠霖",
    "魏子越",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
REQUEST_TIMEOUT = 25
THROTTLE_SECONDS = 0.05
FORCE_DOWNLOAD = bool(os.environ.get("FORCE_DOWNLOAD_ASSETS"))

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


class DownloadError(Exception):
    """Raised when no source could be resolved for a celebrity."""


def load_celebrities() -> List[Dict[str, str]]:
    text = PEOPLE_HTML.read_text(encoding="utf-8")
    pattern = re.compile(
        r'"displayName"\s*:\s*"([^"]+)"\s*,\s*"wiki"\s*:\s*\{\s*"lang"\s*:\s*"([^"]+)"\s*,\s*"title"\s*:\s*"([^"]+)"',
        re.DOTALL,
    )
    ordered: "OrderedDict[str, Dict[str, str]]" = OrderedDict()
    for match in pattern.finditer(text):
        display, lang, title = match.groups()
        ordered.setdefault(
            display,
            {
                "name": display,
                "wiki_lang": lang.strip(),
                "wiki_title": title.strip(),
            },
        )
    return list(ordered.values())


def load_fallback_sources() -> Dict[str, str]:
    fallback: Dict[str, str] = {}
    if not FALLBACK_LIST.exists():
        return fallback
    lines = FALLBACK_LIST.read_text(encoding="utf-8").splitlines()
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        name_part, _, rest = line.partition(",")
        name = name_part.strip()
        if not name:
            continue
        match = re.search(r"https?://\S+", rest)
        if match:
            fallback[name] = match.group(0)
    return fallback


def quote_wiki_title(title: str) -> str:
    return quote(title, safe="")


def fetch_wikipedia_payload(lang: str, title: str) -> Dict:
    encoded = quote_wiki_title(title)
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{encoded}"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise DownloadError(f"Wikipedia summary HTTP {resp.status_code} for {title}")
    data = resp.json()
    if data.get("type") == "https://mediawiki.org/wiki/HyperSwitch/errors/not_found":
        raise DownloadError("Wikipedia page not found")
    return data


def build_wikipedia_source(lang: str, title: str) -> Tuple[str, str, str]:
    data = fetch_wikipedia_payload(lang, title)
    image = (
        (data.get("originalimage") or {}).get("source")
        or (data.get("thumbnail") or {}).get("source")
    )
    page_url = (
        ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
        or ((data.get("content_urls") or {}).get("mobile") or {}).get("page")
        or f"https://{lang}.wikipedia.org/wiki/{quote_wiki_title(title)}"
    )
    provider = f"{lang.upper()} Wikipedia"
    if image:
        return ensure_https(image), page_url, provider
    # fall back to parsing the article HTML for og:image
    html_image = fetch_wikipedia_page_image(page_url)
    if html_image:
        return ensure_https(html_image), page_url, provider
    raise DownloadError("Wikipedia summary missing images")


def fetch_wikipedia_page_image(page_url: str) -> Optional[str]:
    resp = session.get(page_url, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return None
    html = resp.text
    img_match = re.search(
        r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.IGNORECASE
    )
    if not img_match:
        img_match = re.search(
            r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html, re.IGNORECASE
        )
    if not img_match:
        return None
    return ensure_https(img_match.group(1))


def fetch_generic_page_image(page_url: str) -> Tuple[str, str, str]:
    resp = session.get(page_url, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise DownloadError(f"Fallback page HTTP {resp.status_code}")
    html = resp.text
    img_match = re.search(
        r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.IGNORECASE
    )
    if not img_match:
        img_match = re.search(
            r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html, re.IGNORECASE
        )
    if not img_match:
        img_match = re.search(
            r'rel="image_src"[^>]+href="([^"]+)"', html, re.IGNORECASE
        )
    if not img_match:
        raise DownloadError("Fallback page missing og:image")
    image_url = ensure_https(img_match.group(1))
    provider = "Baidu Baike" if "baike.baidu.com" in page_url else urlparse(page_url).netloc
    return image_url, page_url, provider


def fetch_baike_api_image(name: str) -> Tuple[str, str, str]:
    params = {
        "scope": "103",
        "format": "json",
        "appid": "379020",
        "bk_key": name,
        "bk_length": "600",
    }
    resp = session.get(
        "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi",
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise DownloadError(f"Baidu Baike API HTTP {resp.status_code}")
    data = resp.json()
    page_url = data.get("url") or data.get("wapUrl")
    if not page_url:
        guessed = f"https://baike.baidu.com/item/{quote(name, safe='')}"
        return fetch_generic_page_image(guessed)
    page_url = ensure_https(page_url)
    image_url = data.get("image")
    if image_url:
        return ensure_https(image_url), page_url, "Baidu Baike"
    # try parsing the page directly if API did not include image
    return fetch_generic_page_image(page_url)


def ensure_https(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def determine_extension(url: str) -> str:
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext in SUPPORTED_EXTS:
        return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def sanitize_filename(name: str, ext: str) -> str:
    safe = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    safe = safe.replace(" ", "")
    return safe + ext


def download_file(url: str, destination: Path) -> None:
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise DownloadError(f"Image HTTP {resp.status_code}")
    destination.write_bytes(resp.content)


def resolve_source(name: str, wiki_lang: str, wiki_title: str, fallback_map: Dict[str, str]):
    if name in BAIKE_PREFERRED:
        try:
            return fetch_baike_api_image(name)
        except DownloadError:
            pass
    if name in MANUAL_WIKI_OVERRIDES:
        override = MANUAL_WIKI_OVERRIDES[name]
        try:
            return build_wikipedia_source(override["lang"], override["title"])
        except DownloadError:
            pass
    try:
        return build_wikipedia_source(wiki_lang, wiki_title)
    except DownloadError as wiki_error:
        last_error: Optional[Exception] = wiki_error
        fallback_url = fallback_map.get(name)
        if fallback_url:
            try:
                domain = urlparse(fallback_url).netloc
                if "wikipedia.org" in domain:
                    parts = domain.split(".")
                    lang = parts[0]
                    title = urlparse(fallback_url).path.split("/")[-1] or wiki_title
                    return build_wikipedia_source(lang, title)
                return fetch_generic_page_image(fallback_url)
            except DownloadError as fallback_err:
                last_error = fallback_err

        try:
            return fetch_baike_api_image(name)
        except DownloadError as baike_err:
            last_error = baike_err

        raise last_error


def main():
    celebrities = load_celebrities()
    fallback_map = load_fallback_sources()
    if not celebrities:
        print("No celebrities parsed from people.html", file=sys.stderr)
        sys.exit(1)

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    existing_manifest: Dict[str, Dict[str, str]] = {}
    if MANIFEST_JSON.exists():
        try:
            existing_data = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
            existing_manifest = {item["name"]: item for item in existing_data}
        except json.JSONDecodeError:
            existing_manifest = {}
    results = []
    failures = []

    for idx, entry in enumerate(celebrities, 1):
        name = entry["name"]
        prefix = f"[{idx}/{len(celebrities)}] {name}"
        if not FORCE_DOWNLOAD and name in existing_manifest:
            existing_entry = existing_manifest[name]
            existing_path = ROOT / existing_entry.get("path", "")
            if existing_path.exists():
                results.append(existing_entry)
                print(f"{prefix} ... skip (cached)")
                continue
        sys.stdout.write(f"{prefix} ... ")
        sys.stdout.flush()
        did_network = True
        try:
            image_url, page_url, provider = resolve_source(
                name,
                entry.get("wiki_lang", "zh"),
                entry.get("wiki_title", name),
                fallback_map,
            )
            ext = determine_extension(image_url)
            filename = sanitize_filename(name, ext)
            dest = ASSET_DIR / filename
            download_file(image_url, dest)
            results.append(
                {
                    "name": name,
                    "path": f"assets/people/{filename}",
                    "sourceUrl": page_url,
                    "imageUrl": image_url,
                    "provider": provider,
                }
            )
            sys.stdout.write("done\n")
        except Exception as exc:  # noqa: BLE001
            failures.append({"name": name, "error": str(exc)})
            sys.stdout.write(f"failed ({exc})\n")
        if did_network:
            time.sleep(THROTTLE_SECONDS)

    if results:
        with CATALOG_CSV.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=["name", "path", "sourceUrl", "imageUrl", "provider"])
            writer.writeheader()
            writer.writerows(results)
        MANIFEST_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    if failures:
        failure_log = ASSET_DIR / "download_failures.json"
        failure_log.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n{len(failures)} downloads failed. See {failure_log}")
    else:
        failure_path = ASSET_DIR / "download_failures.json"
        if failure_path.exists():
            failure_path.unlink()
        print("\nAll downloads succeeded.")


if __name__ == "__main__":
    main()

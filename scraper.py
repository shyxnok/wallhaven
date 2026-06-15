#!/usr/bin/env python3
"""
Wallhaven.cc Wallpaper Scraper
==============================
Crawls https://wallhaven.cc/latest pages, extracts wallpaper detail links,
fetches full-resolution image URLs, and downloads them with rate limiting.

Uses curl as the HTTP backend (more reliable TLS handling across platforms).

Usage:
    python3 scraper.py                        # scrape page 1, download to ./wallpapers/
    python3 scraper.py -p 1-5                 # scrape pages 1 through 5
    python3 scraper.py -p 3 -d ./images       # page 3 only, save to ./images/
    python3 scraper.py -p 1-3 -n 10           # max 10 wallpapers total
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# ── Constants ──────────────────────────────────────────────────────────────

BASE_URL = "https://wallhaven.cc"
LATEST_URL = f"{BASE_URL}/latest"
API_BASE = "https://wallhaven.cc/api/v1"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

CURL_TIMEOUT = 30
CURL_RETRY = 3


# ── curl helpers ───────────────────────────────────────────────────────────

def _curl_get(
    url: str,
    referer: str | None = None,
    extra_headers: list[str] | None = None,
    output_file: str | None = None,
    timeout: int = CURL_TIMEOUT,
) -> subprocess.CompletedProcess:
    """
    Run curl GET and return the CompletedProcess.

    When *output_file* is given, stdout is that file (-o mode); otherwise
    stdout is captured as the response body.
    """
    cmd = [
        "curl", "-sS", "--fail-with-body",
        "--retry", str(CURL_RETRY),
        "--max-time", str(timeout),
        "-H", f"User-Agent: {UA}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.5",
    ]

    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if extra_headers:
        for h in extra_headers:
            cmd += ["-H", h]

    if output_file:
        cmd += ["-o", output_file]
        # With -o, also write stderr to a temp file so we can check for errors
        result = subprocess.run(
            cmd + [url],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    else:
        result = subprocess.run(
            cmd + [url],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )

    return result


def curl_get_text(url: str, referer: str | None = None, extra_headers: list[str] | None = None) -> str | None:
    """Fetch a URL with curl, return body text or None on failure."""
    extra_h = list(extra_headers) if extra_headers else []
    try:
        r = _curl_get(url, referer=referer, extra_headers=extra_h)
        if r.returncode != 0:
            print(f"    curl error ({r.returncode}): {r.stderr[:200]}")
            return None
        return r.stdout
    except subprocess.TimeoutExpired:
        print(f"    curl timeout for {url}")
        return None


def curl_download(url: str, dest: Path, referer: str | None = None) -> bool:
    """Download a file with curl. Returns True on success."""
    # Also try HTTP fallback for CDN
    urls = [url]
    if url.startswith("https://w.wallhaven.cc"):
        urls.append(url.replace("https://", "http://", 1))

    for attempt, u in enumerate(urls):
        try:
            r = _curl_get(u, referer=referer, output_file=str(dest))
            if attempt == 0 and r.returncode != 0 and len(urls) > 1:
                # First attempt failed, try fallback
                continue
            if r.returncode != 0:
                return False
            # Verify we got an image (check file exists and is non-empty)
            if dest.stat().st_size < 100:
                dest.unlink(missing_ok=True)
                continue
            return True
        except subprocess.TimeoutExpired:
            continue

    return False


# ── API-based fetching (needs API key) ─────────────────────────────────────

def curl_get_json(url: str, api_key: str) -> dict | None:
    """Fetch JSON from wallhaven API."""
    text = curl_get_text(url, extra_headers=[f"X-API-Key: {api_key}"])
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

# ══════════════════════════════════════════════════════════════════════════
# NOTE: The API functions below require a working HTTP client.
# Since this machine's Python SSL is broken and these use curl_get_text
# (which calls curl), they work correctly for users with an API key.
# For HTML scraping (no API key), see the HTML section below.
# ══════════════════════════════════════════════════════════════════════════


def fetch_wallpapers_via_api(
    api_key: str,
    page: int,
    purity: str = "sfw",
    categories: str = "100",
    sorting: str = "date_added",
    order: str = "desc",
    ratios: str | None = None,
    resolutions: str | None = None,
) -> list[dict]:
    """Fetch wallpaper metadata via wallhaven API. Returns list of wallpaper dicts."""
    params_parts = [
        f"page={page}",
        f"purity={purity}",
        f"categories={categories}",
        f"sorting={sorting}",
        f"order={order}",
    ]
    if ratios:
        params_parts.append(f"ratios={ratios}")
    if resolutions:
        params_parts.append(f"resolutions={resolutions}")

    url = f"{API_BASE}/search?{'&'.join(params_parts)}"
    data = curl_get_json(url, api_key)
    if data is None:
        return []
    return data.get("data", [])


def get_image_url_via_api(api_key: str, wallpaper_id: str) -> str | None:
    """Get full image URL from API for a single wallpaper."""
    data = curl_get_json(f"{API_BASE}/w/{wallpaper_id}", api_key)
    if data is None:
        return None
    return data.get("data", {}).get("path")


# ── HTML scraping (no API key needed) ──────────────────────────────────────

def fetch_listing_page(page: int) -> BeautifulSoup | None:
    """Fetch a /latest listing page, return parsed HTML or None."""
    url = f"{LATEST_URL}?page={page}"
    html = curl_get_text(url, referer=BASE_URL + "/")
    if html is None:
        return None
    return BeautifulSoup(html, "html.parser")


def extract_wallpaper_ids(soup: BeautifulSoup) -> list[str]:
    """Extract wallpaper IDs from a listing page."""
    ids: list[str] = []

    # Strategy 1: data-wallpaper-id attribute
    for el in soup.select("[data-wallpaper-id]"):
        wid = el.get("data-wallpaper-id")
        if wid and isinstance(wid, str) and wid not in ids:
            ids.append(wid)

    # Strategy 2: links to /w/<id>
    if not ids:
        for a in soup.find_all("a", href=True):
            m = re.match(r"^/w/([a-z0-9]+)$", str(a["href"]))
            if m:
                wid = m.group(1)
                if wid not in ids:
                    ids.append(wid)

    return ids


def fetch_full_image_url(wallpaper_id: str) -> str | None:
    """
    Visit a wallpaper detail page and extract the full-resolution image URL.
    Returns the image URL or None.
    """
    url = f"{BASE_URL}/w/{wallpaper_id}"
    html = curl_get_text(url, referer=BASE_URL + "/")
    if html is None:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Primary: <img id="wallpaper" src="...">
    img = soup.find("img", id="wallpaper")
    if img and img.get("src"):
        return str(img["src"])

    # Fallback: any img with the wallpaper id in src and "full" in path
    for img in soup.find_all("img", src=True):
        if wallpaper_id in img["src"] and "full" in img["src"]:
            return str(img["src"])

    return None


# ── Download ───────────────────────────────────────────────────────────────

def download_image(
    image_url: str,
    save_dir: Path,
    wallpaper_id: str = "",
    skip_existing: bool = True,
) -> bool:
    """Download an image file. Returns True on success."""
    # Derive filename from URL
    parsed = urlparse(image_url)
    filename = Path(parsed.path).name
    if not filename or "." not in filename:
        filename = f"{hashlib.md5(image_url.encode()).hexdigest()[:12]}.jpg"

    filepath = save_dir / filename

    if skip_existing and filepath.exists():
        print(f"  [SKIP] Already exists: {filename}")
        return False

    referer = f"https://wallhaven.cc/w/{wallpaper_id}" if wallpaper_id else BASE_URL + "/"

    ok = curl_download(image_url, filepath, referer=referer)
    if ok:
        size_mb = filepath.stat().st_size / (1024 * 1024)
        print(f"  [OK] {filename} ({size_mb:.1f} MB)")
        return True
    else:
        print(f"  [FAIL] {filename}")
        filepath.unlink(missing_ok=True)
        return False


# ── Orchestration ──────────────────────────────────────────────────────────

def run_scraper(
    pages: range,
    save_dir: Path,
    api_key: str | None = None,
    purity: str = "sfw",
    categories: str = "100",
    resolutions: str | None = None,
    ratios: str | None = None,
    max_wallpapers: int | None = None,
    delay_page: float = 2.0,
    delay_image: float = 1.0,
    delay_detail: float = 1.0,
) -> tuple[int, int]:
    """Main scraper. Returns (downloaded, skipped)."""
    save_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    skipped = 0
    total_pages = len(pages)

    for page_idx, page in enumerate(pages, start=1):
        print(f"\n{'='*60}")
        print(f"Page {page} ({page_idx}/{total_pages})")
        print(f"{'='*60}")

        # ── Fetch wallpaper list ──
        if api_key:
            wallpapers = fetch_wallpapers_via_api(
                api_key, page,
                purity=purity, categories=categories,
                resolutions=resolutions, ratios=ratios,
            )
            wallpaper_ids = [w["id"] for w in wallpapers]
            print(f"Found {len(wallpaper_ids)} wallpapers (API)")
        else:
            soup = fetch_listing_page(page)
            if soup is None:
                print(f"[ERROR] Failed to fetch page {page}, skipping.")
                continue
            wallpaper_ids = extract_wallpaper_ids(soup)
            print(f"Found {len(wallpaper_ids)} wallpaper IDs (HTML)")

        if not wallpaper_ids:
            print("No wallpapers on this page — stopping.")
            break

        # ── Process each wallpaper ──
        for idx, wid in enumerate(wallpaper_ids, start=1):
            processed = downloaded + skipped
            if max_wallpapers is not None and processed >= max_wallpapers:
                print(f"\nReached --max limit ({max_wallpapers}). Stopping.")
                return downloaded, skipped

            print(f"\n[{idx}/{len(wallpaper_ids)}] {wid}")

            # Get full image URL
            if api_key:
                img_url = get_image_url_via_api(api_key, wid)
            else:
                img_url = fetch_full_image_url(wid)
                time.sleep(delay_detail)

            if not img_url:
                print(f"  [SKIP] No image URL found")
                skipped += 1
                continue

            # Download
            success = download_image(img_url, save_dir, wallpaper_id=wid)
            if success:
                downloaded += 1
            else:
                skipped += 1

            time.sleep(delay_image)

        time.sleep(delay_page)

    return downloaded, skipped


# ── CLI ────────────────────────────────────────────────────────────────────

def parse_page_range(arg: str) -> range:
    """Parse '3' or '1-5' into a range (1-indexed, inclusive)."""
    if "-" in arg:
        start, end = arg.split("-", 1)
        start, end = int(start), int(end)
        if start < 1:
            raise ValueError("Page numbers start at 1")
        if end < start:
            raise ValueError("End page must be >= start page")
        return range(start, end + 1)
    return range(int(arg), int(arg) + 1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wallhaven.cc Wallpaper Scraper (curl backend)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Scrape page 1 to ./wallpapers/
  %(prog)s -p 1-5                   Scrape pages 1 through 5
  %(prog)s -p 3 -d ./images         Scrape page 3, save to ./images/
  %(prog)s -p 1-5 -n 20             Max 20 wallpapers total
  %(prog)s -p 1 -k YOUR_API_KEY     Use wallhaven API (faster)
        """,
    )
    parser.add_argument("-p", "--pages", default="1",
                        help="Page or range, e.g. '3' or '1-5' (default: 1)")
    parser.add_argument("-d", "--dir", default="./wallpapers",
                        help="Save directory (default: ./wallpapers)")
    parser.add_argument("-k", "--api-key",
                        help="Wallhaven API key (optional)")
    parser.add_argument("-c", "--categories", default="100",
                        help="Category bits: 100=General, 010=Anime, 001=People, 111=All")
    parser.add_argument("--purity", default="sfw",
                        choices=["sfw", "sketchy", "nsfw"],
                        help="Purity filter (default: sfw; sketchy/nsfw need API key)")
    parser.add_argument("-r", "--resolutions",
                        help="Comma-separated resolutions (API only)")
    parser.add_argument("--ratios",
                        help="Comma-separated ratios (API only)")
    parser.add_argument("-n", "--max", type=int, default=None,
                        help="Max wallpapers to download")
    parser.add_argument("--delay-page", type=float, default=2.0,
                        help="Seconds between pages (default: 2)")
    parser.add_argument("--delay-image", type=float, default=1.0,
                        help="Seconds between downloads (default: 1)")
    parser.add_argument("--delay-detail", type=float, default=1.0,
                        help="Seconds between detail pages (default: 1)")

    args = parser.parse_args()

    # Validation
    if args.purity in ("sketchy", "nsfw") and not args.api_key:
        print("NSFW/sketchy requires an API key. Use --api-key or stay with --purity sfw.")
        sys.exit(1)
    if (args.resolutions or args.ratios) and not args.api_key:
        print("Resolution/ratio filtering requires an API key.")
        sys.exit(1)

    try:
        pages = parse_page_range(args.pages)
    except ValueError as e:
        print(f"Invalid page range: {e}")
        sys.exit(1)

    save_dir = Path(args.dir).resolve()

    print("=" * 58)
    print("  Wallhaven.cc Wallpaper Scraper (curl)")
    print("=" * 58)
    print(f"  Pages:       {pages.start} → {pages.stop - 1}")
    print(f"  Save dir:    {save_dir}")
    print(f"  API key:     {'yes' if args.api_key else 'no (HTML scrape)'}")
    print(f"  Categories:  {args.categories}")
    print(f"  Purity:      {args.purity}")
    if args.resolutions:
        print(f"  Resolutions: {args.resolutions}")
    if args.ratios:
        print(f"  Ratios:      {args.ratios}")
    if args.max:
        print(f"  Max images:  {args.max}")
    print("=" * 58)

    try:
        downloaded, skipped = run_scraper(
            pages=pages,
            save_dir=save_dir,
            api_key=args.api_key or None,
            purity=args.purity,
            categories=args.categories,
            resolutions=args.resolutions,
            ratios=args.ratios,
            max_wallpapers=args.max,
            delay_page=args.delay_page,
            delay_image=args.delay_image,
            delay_detail=args.delay_detail,
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(0)

    print(f"\n{'='*58}")
    print(f"  Done! Downloaded: {downloaded}  |  Skipped: {skipped}")
    print(f"  Files in: {save_dir}")
    print(f"{'='*58}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
scrape_wayback_harris.py — Scrape Kim Ogg era Harris County DA press releases
from the Wayback Machine (harriscountyda.com/news_updates).

Background: Kim Ogg was Harris County DA from Jan 2017 – Dec 2024. Her office
published hundreds of press releases at harriscountyda.com/news_updates, which
is no longer live. The site had paginated listing pages (page=0..N) where each
page embedded full release text inline (no individual article URLs). Sean Teare
took office in Jan 2025 and launched a new site (dao.harriscountytx.gov) with
only ~12 releases.

Strategy:
  1. Find all archived listing pages via CDX API
  2. For each listing page snapshot, extract individual release blocks
  3. Deduplicate by title+date and save each as a .txt file

Usage:
  python3 scrape_wayback_harris.py
  python3 scrape_wayback_harris.py --delay 2.0
  python3 scrape_wayback_harris.py --limit 200
  python3 scrape_wayback_harris.py --dry-run
"""

import argparse
import hashlib
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─── Config ───────────────────────────────────────────────────────────────────

COUNTY_KEY = "harris_tx"
OUT_DIR = Path("text") / COUNTY_KEY
LINKS_FILE = Path("links") / COUNTY_KEY / "links.txt"
WAYBACK_BASE = "https://web.archive.org"

# CDX query: all archived listing pages at harriscountyda.com/news_updates
CDX_URL = (
    "https://web.archive.org/cdx/search/cdx"
    "?url=harriscountyda.com/news_updates*"
    "&output=json"
    "&fl=timestamp,original,statuscode"
    "&collapse=urlkey"
    "&from=20170101"
    "&to=20241231"
    "&filter=statuscode:200"
    "&limit=200"
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get(url: str, delay: float = 1.0) -> requests.Response:
    time.sleep(delay)
    resp = requests.get(url, timeout=30, headers={"User-Agent": "research-scraper/1.0"})
    resp.raise_for_status()
    return resp


def slug(title: str) -> str:
    """Convert a release title to a filesystem-safe slug."""
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:100]


def extract_releases_from_listing(html: str) -> list[dict]:
    """
    Extract individual press release blocks from a harriscountyda.com/news_updates page.
    The page embeds full release text in a repeating pattern:
      <h2 or h3> Title </h2>
      <p> Body text... </p>
      ...
    Returns list of {"title": str, "body": str}
    """
    s = BeautifulSoup(html, "html.parser")

    # Remove nav, header, footer boilerplate
    for tag in s(["nav", "header", "footer", "script", "style"]):
        tag.decompose()

    releases = []

    # Releases are grouped under heading tags (h2/h3) in the main content area
    main = s.find("main") or s.find("div", class_=re.compile(r"content|main|body", re.I)) or s

    headings = main.find_all(["h2", "h3"])
    for h in headings:
        title = h.get_text(strip=True)
        if len(title) < 20:
            continue  # skip short nav headings
        # Collect sibling paragraphs until the next heading
        body_parts = []
        for sib in h.find_next_siblings():
            if sib.name in ("h2", "h3"):
                break
            text = sib.get_text(separator=" ", strip=True)
            if text:
                body_parts.append(text)
        body = "\n\n".join(body_parts)
        if len(body) > 100:  # only keep releases with substantive text
            releases.append({"title": title, "body": body})

    return releases


def release_filename(title: str, timestamp: str) -> str:
    """Generate a unique filename from title + timestamp."""
    date = timestamp[:8]  # YYYYMMDD
    s = f"{date}_{slug(title)}.txt"
    return s


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Seconds between requests (default 1.5)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max releases to save (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse but don't write files")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)

    print("Fetching CDX index of archived listing pages...")
    resp = requests.get(CDX_URL, timeout=60)
    rows = resp.json()
    listing_pages = [(r[0], r[1]) for r in rows[1:]]  # skip header
    print(f"Found {len(listing_pages)} archived listing page snapshots")

    seen_titles: set[str] = set()
    saved_urls: list[str] = []
    saved = skipped = failed = 0

    for ts, orig_url in listing_pages:
        wb_url = f"{WAYBACK_BASE}/web/{ts}/{orig_url}"
        print(f"\n  Fetching: {orig_url} (snapshot {ts[:8]})")
        try:
            resp = get(wb_url, delay=args.delay)
        except Exception as e:
            print(f"    ERROR fetching {wb_url}: {e}")
            failed += 1
            continue

        releases = extract_releases_from_listing(resp.text)
        print(f"    Found {len(releases)} releases on page")

        for rel in releases:
            title = rel["title"]
            title_key = title.lower().strip()

            if title_key in seen_titles:
                skipped += 1
                continue
            seen_titles.add(title_key)

            fname = release_filename(title, ts)
            full_text = f"{title}\n\n{rel['body']}"
            fake_url = f"https://www.harriscountyda.com/news_updates/{slug(title)}"
            saved_urls.append(fake_url)

            if not args.dry_run:
                (OUT_DIR / fname).write_text(full_text + "\n")
            saved += 1
            print(f"    + {fname[:70]}")

            if args.limit and saved >= args.limit:
                print(f"\n  Hit --limit {args.limit}, stopping.")
                break

        if args.limit and saved >= args.limit:
            break

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Done.")
    print(f"  Saved: {saved}  Duplicates skipped: {skipped}  Page fetch errors: {failed}")

    if not args.dry_run and saved_urls:
        # Append new URLs to links file (avoid duplicates)
        existing = set()
        if LINKS_FILE.exists():
            existing = set(LINKS_FILE.read_text().splitlines())
        new_urls = [u for u in saved_urls if u not in existing]
        if new_urls:
            with LINKS_FILE.open("a") as f:
                f.write("\n".join(new_urls) + "\n")
            print(f"  Added {len(new_urls)} URLs to {LINKS_FILE}")


if __name__ == "__main__":
    main()

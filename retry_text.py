#!/usr/bin/env python3
"""
Retry text fetching for a county, skipping URLs that already have a text file.
Usage: python3 retry_text.py <county_key> [delay_seconds]
"""

import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
    import io
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip, deflate",
}

import re
from urllib.parse import urlparse

session = requests.Session()
session.headers.update(HEADERS)


def slug(url):
    path = urlparse(url).path.strip("/").replace("/", "_")
    path = re.sub(r"[^a-zA-Z0-9_\-]", "_", path)
    return path[:120] or "index"


def extract_text_from_response(resp):
    content_type = resp.headers.get("Content-Type", "")
    is_pdf = "pdf" in content_type.lower() or resp.content[:4] == b"%PDF"
    if is_pdf:
        if not PDFPLUMBER_AVAILABLE:
            return "[PDF — install pdfplumber]"
        pages = []
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
        return "\n\n".join(pages)
    s = BeautifulSoup(resp.text, "html.parser")
    for tag in s(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return s.get_text(separator="\n", strip=True)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 retry_text.py <county_key> [delay_seconds]")
        sys.exit(1)

    county_key = sys.argv[1]
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0

    links_file = Path("links") / county_key / "links.txt"
    text_dir = Path("text") / county_key

    if not links_file.exists():
        print(f"No links file found for {county_key}")
        sys.exit(1)

    all_links = [l.strip() for l in links_file.read_text().splitlines() if l.strip()]

    # Find which ones are missing
    missing = []
    for url in all_links:
        out = text_dir / (slug(url) + ".txt")
        if not out.exists():
            missing.append(url)

    print(f"{county_key}: {len(all_links)} total links, {len(missing)} missing text files")
    print(f"Using {delay}s delay between requests\n")

    saved = 0
    failed = 0
    for i, url in enumerate(missing, 1):
        try:
            time.sleep(delay)
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            text = extract_text_from_response(resp)
            out = text_dir / (slug(url) + ".txt")
            out.write_text(text.strip() + "\n")
            saved += 1
            if i % 100 == 0:
                print(f"  [{i}/{len(missing)}] {saved} saved, {failed} failed so far")
        except Exception as e:
            failed += 1
            print(f"  failed {url}: {e}")

    print(f"\nDone. Saved {saved}/{len(missing)}, failed {failed}")


if __name__ == "__main__":
    main()

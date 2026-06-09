#!/usr/bin/env python3
"""
Scrape press releases from prosecutor offices in the top 10 most populous US counties.

Outputs:
  links/<county_key>/links.txt   — one URL per line
  text/<county_key>/<slug>.txt   — raw text of each release
"""

import csv
import io
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    # Exclude brotli so requests can decode the body reliably
    "Accept-Encoding": "gzip, deflate",
}
REQUEST_DELAY = 1.5  # seconds between requests
MAX_PAGES = 5000     # high cap — date-based stopping is the real limit
CUTOFF_YEAR = 2000   # stop paginating when releases pre-date this year

session = requests.Session()
session.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get(url, **kwargs):
    time.sleep(REQUEST_DELAY)
    resp = session.get(url, timeout=20, **kwargs)
    resp.raise_for_status()
    return resp


def soup(resp):
    return BeautifulSoup(resp.text, "html.parser")


def slug(url):
    """Turn a URL into a safe filename stem."""
    path = urlparse(url).path.strip("/").replace("/", "_")
    path = re.sub(r"[^a-zA-Z0-9_\-]", "_", path)
    return path[:120] or "index"


def links_past_cutoff(hrefs):
    """True if any of the scraped URLs contain a year older than CUTOFF_YEAR.
    Only fires when URLs themselves embed a year (e.g. /2000/01/15/ or -1999-).
    Ignores footer/copyright years that appear in page prose."""
    for href in hrefs:
        m = re.search(r"/(1[89]\d{2}|20\d{2})/", href)
        if m and int(m.group(1)) < CUTOFF_YEAR:
            return True
    return False


def save_links(county_key, links):
    out = Path("links") / county_key / "links.txt"
    out.write_text("\n".join(links) + "\n")
    print(f"  [{county_key}] saved {len(links)} links → {out}")


def save_text(county_key, url, text):
    out = Path("text") / county_key / (slug(url) + ".txt")
    out.write_text(text.strip() + "\n")


def extract_text(resp):
    """Pull readable text from an HTML response."""
    s = soup(resp)
    for tag in s(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return s.get_text(separator="\n", strip=True)


def extract_text_from_response(resp):
    """Detect PDF vs HTML and extract text accordingly."""
    content_type = resp.headers.get("Content-Type", "")
    is_pdf = "pdf" in content_type.lower() or resp.content[:4] == b"%PDF"
    if is_pdf:
        if not PDFPLUMBER_AVAILABLE:
            return "[PDF — install pdfplumber to extract text]"
        pages = []
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
        return "\n\n".join(pages)
    return extract_text(resp)


# ---------------------------------------------------------------------------
# Per-site scrapers — links
# ---------------------------------------------------------------------------

def scrape_links_los_angeles():
    """Drupal 7, ?page=N pagination, 1 release per page. Paginate until no next button."""
    base = "https://da.lacounty.gov/media/news"
    links = []
    for page in range(MAX_PAGES):
        url = base if page == 0 else f"{base}?page={page}"
        resp = get(url)
        s = soup(resp)
        # Try multiple selectors — site structure has varied over time
        items = s.select(
            ".views-row h2 a, .views-row h3 a, "
            "h2 a[href*='/media/news/'], h3 a[href*='/media/news/'], "
            "h3.node-title a"
        )
        if not items:
            break
        for a in items:
            href = urljoin(base, a["href"])
            if href not in links:
                links.append(href)
        if not s.select(".pager-next a, li.pager-next a"):
            break
    return links


def scrape_links_cook():
    """Drupal 10, ?page=N. Paginate until no next button."""
    base = "https://www.cookcountystatesattorney.org/news"
    links = []
    for page in range(MAX_PAGES):
        url = base if page == 0 else f"{base}?page={page}"
        resp = get(url)
        s = soup(resp)
        items = s.select("h3 a, .views-row a, article a[href*='/news/']")
        if not items:
            break
        for a in items:
            href = urljoin(base, a["href"])
            if urlparse(href).netloc == urlparse(base).netloc and href not in links:
                links.append(href)
        if not s.select(".pager__item--next a"):
            break
    return links


def scrape_links_harris():
    """DNN/ASP.NET static listing."""
    base = "https://dao.harriscountytx.gov/Newsroom/News-Releases"
    links = []
    resp = get(base)
    s = soup(resp)
    for a in s.select("a[href*='News-Releases']"):
        href = urljoin(base, a["href"])
        if href != base and href not in links:
            links.append(href)
    return links


def scrape_links_maricopa():
    """CivicPlus archive — 1,100+ releases on a single static page.
    Archive: /civicalerts.aspx?ARC=L&What=1&CC=0&intArchCatID=41
    Individual releases: /CivicAlerts.aspx?AID=<id>
    No Playwright needed."""
    from urllib.parse import parse_qs, urlparse as _up
    archive = (
        "https://maricopacountyattorney.org/civicalerts.aspx"
        "?ARC=L&What=1&CC=0&intArchCatID=41&From=CID%3d41"
    )
    base = "https://maricopacountyattorney.org"
    links = []
    seen_aids = set()
    resp = get(archive)
    s = soup(resp)
    for a in s.find_all("a", href=True):
        href = a["href"]
        if "CivicAlerts" not in href or "AID=" not in href:
            continue
        qs = parse_qs(_up(href).query)
        aid = qs.get("AID", [None])[0]
        if aid and aid not in seen_aids:
            seen_aids.add(aid)
            links.append(f"{base}/CivicAlerts.aspx?AID={aid}")
    return links


def scrape_links_san_diego():
    """ASP.NET — releases are served as PDFs via GetNewsroomFile?UID=..."""
    base = "https://www.sdcda.org/office/newsroom/"
    links = []
    resp = get(base)
    s = soup(resp)
    for a in s.find_all("a", href=True):
        href = a["href"]
        # Only keep individual release PDFs, not nav/listing pages
        if "GetNewsroomFile" in href:
            full = urljoin(base, href)
            if full not in links:
                links.append(full)
    return links


def scrape_links_orange():
    """WordPress, ~410 pages of releases at /press/page/N/. Paginate until no items."""
    base = "https://ocdistrictattorney.gov/press/"
    links = []
    for page in range(1, MAX_PAGES + 1):
        url = base if page == 1 else f"{base}page/{page}/"
        try:
            resp = get(url)
        except requests.HTTPError:
            break
        s = soup(resp)
        items = s.select("h3 a[href*='ocdistrictattorney.gov/press/']")
        if not items:
            break
        for a in items:
            href = urljoin(base, a["href"])
            if href not in links:
                links.append(href)
    return links


def scrape_links_miami_dade():
    """WordPress — releases live at /press-release/slug/. Paginate until no items or no next."""
    base = "https://miamisao.com/news/press-release-news/"
    links = []
    for page in range(1, MAX_PAGES + 1):
        url = base if page == 1 else f"{base}page/{page}/"
        try:
            resp = get(url)
        except requests.HTTPError:
            break
        s = soup(resp)
        items = s.select("h3 a[href*='/press-release/']")
        if not items:
            break
        page_links = []
        for a in items:
            href = urljoin(base, a["href"])
            if href not in links:
                links.append(href)
                page_links.append(href)
        if links_past_cutoff(page_links):
            break
        if not s.select("a.next, .nav-previous"):
            break
    return links


def scrape_links_dallas():
    """Percussion CMS, year-based archive pages back to 2000."""
    bases = [
        "https://www.dallascounty.org/government/district-attorney/press-releases/",
    ]
    for yr in range(0, 27):  # 00 through 26
        bases.append(
            f"https://www.dallascounty.org/government/district-attorney/"
            f"press-releases/press-releases-{yr:02d}.php"
        )
    links = []
    for url in bases:
        try:
            resp = get(url)
        except requests.HTTPError:
            continue
        s = soup(resp)
        for a in s.select("a[href]"):
            href = urljoin(url, a["href"])
            if "press-release" in href.lower() and href not in links:
                links.append(href)
    return links


def scrape_links_kings():
    """Brooklyn DA — releases at brooklynda.org/YYYY/MM/DD/slug/.
    Main page has current year; year archive pages cover prior years back to CUTOFF_YEAR."""
    year_pages = ["https://www.brooklynda.org/press-releases/"]
    for yr in range(2025, CUTOFF_YEAR - 1, -1):
        year_pages.append(f"https://www.brooklynda.org/{yr}-press-releases/")
    links = []
    for page_url in year_pages:
        try:
            resp = get(page_url)
        except requests.HTTPError:
            continue
        s = soup(resp)
        page_links = []
        for a in s.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(page_url, href)
            if re.search(r"brooklynda\.org/20\d{2}/\d{2}/\d{2}/", href) and href not in links:
                links.append(href)
                page_links.append(href)
        if links_past_cutoff(page_links):
            break
    return links


def scrape_links_riverside():
    """Cloudflare-protected — requires Playwright with headless Chromium.
    Also fetches and saves text for each release within the same browser session
    so Cloudflare cookies are reused.
    Falls back to a warning if Playwright is not installed."""
    base = "https://rivcoda.org/news-media-archives"

    if not PLAYWRIGHT_AVAILABLE:
        print("  [riverside_ca] Playwright not installed. Run: pip3 install playwright && playwright install chromium")
        return []

    links = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        try:
            page.goto(base, wait_until="networkidle", timeout=30000)
        except PWTimeout:
            print("  [riverside_ca] page load timed out")
            browser.close()
            return []

        if "just a moment" in page.title().lower() or "checking your browser" in page.content().lower():
            print("  [riverside_ca] Cloudflare challenge not bypassed in headless mode — try headless=False")
            browser.close()
            return []

        # Paginate through listing pages
        prev_count = -1
        while True:
            anchors = page.query_selector_all("a[href]")
            for a in anchors:
                href = a.get_attribute("href") or ""
                if not href:
                    continue
                full = urljoin(base, href)
                if urlparse(full).netloc == urlparse(base).netloc and full not in links:
                    if any(kw in full.lower() for kw in ["news", "release", "press", "media", "article"]):
                        links.append(full)

            load_more = page.query_selector(
                "a:has-text('Next'), a:has-text('Load More'), button:has-text('Load More'), .pager-next a"
            )
            if load_more and len(links) != prev_count:
                prev_count = len(links)
                try:
                    load_more.click()
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    break
            else:
                break

        # Fetch text for each release using the same browser context (preserves CF cookies)
        text_dir = Path("text") / "riverside_ca"
        saved = 0
        for url in links:
            try:
                time.sleep(REQUEST_DELAY)
                page.goto(url, wait_until="networkidle", timeout=20000)
                # Strip nav/footer noise via JS then grab innerText
                text = page.evaluate("""() => {
                    ['script','style','nav','footer','header'].forEach(t =>
                        document.querySelectorAll(t).forEach(e => e.remove()));
                    return document.body.innerText;
                }""")
                out = text_dir / (slug(url) + ".txt")
                out.write_text(text.strip() + "\n")
                saved += 1
            except Exception as e:
                print(f"  [text/riverside_ca] failed {url}: {e}")

        print(f"  [riverside_ca] saved text for {saved}/{len(links)} releases")
        browser.close()

    return links


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

LINK_SCRAPERS = {
    "los_angeles_ca": scrape_links_los_angeles,
    "cook_il":        scrape_links_cook,
    "harris_tx":      scrape_links_harris,
    "maricopa_az":    scrape_links_maricopa,
    "san_diego_ca":   scrape_links_san_diego,
    "orange_ca":      scrape_links_orange,
    "miami_dade_fl":  scrape_links_miami_dade,
    "dallas_tx":      scrape_links_dallas,
    "kings_ny":       scrape_links_kings,
    "riverside_ca":   scrape_links_riverside,
}


# ---------------------------------------------------------------------------
# Text scraping
# ---------------------------------------------------------------------------

def scrape_text(county_key, links):
    count = 0
    for url in links:
        try:
            resp = get(url)
            text = extract_text_from_response(resp)
            save_text(county_key, url, text)
            count += 1
        except Exception as e:
            print(f"  [text/{county_key}] failed {url}: {e}")
    print(f"  [{county_key}] saved text for {count}/{len(links)} releases")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_counties():
    with open("counties.csv") as f:
        return list(csv.DictReader(f))


def main():
    counties = load_counties()
    targets = sys.argv[1:] if sys.argv[1:] else [c["folder_key"] for c in counties]

    for county in counties:
        key = county["folder_key"]
        if key not in targets:
            continue

        print(f"\n=== {county['county']}, {county['state']} ({key}) ===")

        scraper = LINK_SCRAPERS.get(key)
        if not scraper:
            print(f"  No scraper defined for {key}, skipping.")
            continue

        try:
            links = scraper()
            save_links(key, links)
        except Exception as e:
            print(f"  [{key}] link scrape failed: {e}")
            continue

        # Riverside fetches text inside its own Playwright session
        if key == "riverside_ca":
            pass
        elif links:
            scrape_text(key, links)
        else:
            print(f"  [{key}] no links found — check site structure or JS rendering")

    print("\nDone.")


if __name__ == "__main__":
    main()

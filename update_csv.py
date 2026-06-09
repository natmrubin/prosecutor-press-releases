#!/usr/bin/env python3
"""
After scraping, count links and extract date ranges from text files,
then write results back to counties.csv.
"""

import csv
import re
from datetime import datetime
from pathlib import Path

DATE_PATTERNS = [
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
    r"\b\d{1,2}/\d{1,2}/\d{4}\b",
    r"\b\d{4}-\d{2}-\d{2}\b",
]

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_date(s):
    s = s.strip()
    for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def extract_dates_from_file(path):
    text = path.read_text(errors="ignore")
    dates = []
    for pat in DATE_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            d = parse_date(m.group())
            if d and 2000 <= d.year <= 2030:
                dates.append(d)
    return dates


def process_county(folder_key):
    links_file = Path("links") / folder_key / "links.txt"
    text_dir = Path("text") / folder_key

    num_releases = 0
    if links_file.exists():
        lines = [l for l in links_file.read_text().splitlines() if l.strip()]
        num_releases = len(lines)

    all_dates = []
    for txt_file in text_dir.glob("*.txt"):
        all_dates.extend(extract_dates_from_file(txt_file))

    earliest = min(all_dates).isoformat() if all_dates else ""
    latest   = max(all_dates).isoformat() if all_dates else ""
    return num_releases, earliest, latest


def main():
    with open("counties.csv") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        key = row["folder_key"]
        print(f"Processing {key}...")
        num, earliest, latest = process_county(key)
        row["num_releases"] = num
        row["earliest_release"] = earliest
        row["latest_release"] = latest
        print(f"  {num} releases, {earliest} – {latest}")

    fieldnames = list(rows[0].keys())
    with open("counties.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print("\ncounties.csv updated.")


if __name__ == "__main__":
    main()

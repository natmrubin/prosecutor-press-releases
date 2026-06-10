#!/usr/bin/env python3
"""
classify.py — Classify prosecutor press releases using Claude API.

Usage:
  python3 classify.py                     # sample ~100 files, write classification_sample.csv
  python3 classify.py --all               # classify all files, write classifications_full.csv
  python3 classify.py --county kings_ny   # classify one county only
  python3 classify.py --output myfile.csv # custom output path
"""

import argparse
import csv
import os
import random
import sys
from pathlib import Path
from typing import Optional

import json
import re

import anthropic

# ─── Taxonomy ──────────────────────────────────────────────────────────────────

ANNOUNCEMENT_TYPES = [
    "Conviction",
    "Arraignment",
    "Indictment",
    "Sentencing",
    "Arrest",
    "Charges Filed",
    "Office / Policy Announcement",
    "Award / Recognition",
    "Media Advisory",
    "Community / Prevention",
    "Statement",
    "Scam Alert / Public Warning",
    "Other",
]

CRIME_TYPES = [
    "Violent (murder / assault / robbery / weapons)",
    "Sex crimes",
    "White collar / Financial (fraud / embezzlement / tax evasion / deed theft)",
    "Drug crimes",
    "Human trafficking",
    "Child crimes (CSAM / child abuse)",
    "Property crimes",
    "Public corruption",
    "Other",
]

SYSTEM_PROMPT = f"""You are a legal document classifier. You will be given the text of a press release from a prosecutor's or district attorney's office.

Classify the press release into exactly one of these announcement types:
{chr(10).join(f"  - {t}" for t in ANNOUNCEMENT_TYPES)}

If the announcement type is "Conviction", also classify the primary crime type from this list:
{chr(10).join(f"  - {t}" for t in CRIME_TYPES)}

If the announcement covers multiple crimes, pick the most serious or most prominently featured one.

If the announcement type is NOT "Conviction", set crime_type to null.

Respond with a JSON object with exactly two keys: "announcement_type" and "crime_type". No preamble or commentary — JSON only."""

# ─── Helpers ──────────────────────────────────────────────────────────────────

COUNTIES = [
    "los_angeles_ca",
    "cook_il",
    "harris_tx",
    "maricopa_az",
    "san_diego_ca",
    "orange_ca",
    "miami_dade_fl",
    "dallas_tx",
    "kings_ny",
    "riverside_ca",
]


MAX_PER_COUNTY = 1000  # cap for counties with >1000 text files


def get_all_text_files(county: Optional[str] = None) -> list[tuple[str, str]]:
    """Return list of (county_key, filepath) for all .txt files.

    Counties with more than MAX_PER_COUNTY files are randomly capped at that limit.
    """
    text_root = Path("text")
    results = []
    counties = [county] if county else COUNTIES
    for c in counties:
        county_dir = text_root / c
        if not county_dir.exists():
            print(f"  Warning: {county_dir} not found, skipping", file=sys.stderr)
            continue
        files = [(c, str(f)) for f in sorted(county_dir.glob("*.txt"))]
        total = len(files)
        if total > MAX_PER_COUNTY:
            files = random.sample(files, MAX_PER_COUNTY)
            print(f"  {c}: randomly sampled {MAX_PER_COUNTY} of {total} files")
        results.extend(files)
    return results


def sample_files(
    all_files: list[tuple[str, str]], n: int = 100
) -> list[tuple[str, str]]:
    """Sample n files roughly proportionally across counties."""
    from collections import defaultdict

    by_county: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for county, path in all_files:
        by_county[county].append((county, path))

    sampled = []
    per_county = max(1, n // len(by_county))
    remainder = n - per_county * len(by_county)

    for i, (county, files) in enumerate(sorted(by_county.items())):
        k = per_county + (1 if i < remainder else 0)
        sampled.extend(random.sample(files, min(k, len(files))))

    random.shuffle(sampled)
    return sampled[:n]


def read_text(path: str, max_chars: int = 8000) -> str:
    """Read and truncate a text file."""
    try:
        text = Path(path).read_text(errors="replace").strip()
        return text[:max_chars]
    except Exception as e:
        return f"[Error reading file: {e}]"


def classify_one(client: anthropic.Anthropic, text: str) -> dict:
    """Call Claude to classify a single press release."""
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    data = json.loads(raw)
    return {
        "announcement_type": data.get("announcement_type", "Other"),
        "crime_type": data.get("crime_type") or "",
    }


def main():
    parser = argparse.ArgumentParser(description="Classify prosecutor press releases.")
    parser.add_argument("--all", action="store_true", help="Classify all files (not just a sample)")
    parser.add_argument("--county", help="Restrict to a single county key")
    parser.add_argument("--sample", type=int, default=100, help="Sample size (default: 100)")
    parser.add_argument("--output", help="Output CSV path")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for sampling")
    args = parser.parse_args()

    random.seed(args.seed)

    # Determine output path
    if args.output:
        output_path = args.output
    elif args.all:
        output_path = "classifications_full.csv"
    elif args.county:
        output_path = f"classifications_{args.county}.csv"
    else:
        output_path = "classification_sample.csv"

    # Gather files
    all_files = get_all_text_files(args.county)
    print(f"Found {len(all_files)} total text files")

    if args.all:
        files_to_classify = all_files
    else:
        files_to_classify = sample_files(all_files, args.sample)
        print(f"Sampled {len(files_to_classify)} files for classification")

    # Init client
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    # Check for existing output to resume
    done: set[str] = set()
    write_header = True
    if Path(output_path).exists():
        with open(output_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                done.add(row["filename"])
        write_header = False
        print(f"Resuming — {len(done)} already classified in {output_path}")

    remaining = [(c, p) for c, p in files_to_classify if Path(p).name not in done]
    print(f"{len(remaining)} files to classify\n")

    # Write CSV
    with open(output_path, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=["filename", "county", "announcement_type", "crime_type"],
        )
        if write_header:
            writer.writeheader()

        for i, (county, path) in enumerate(remaining, 1):
            filename = Path(path).name
            text = read_text(path)

            try:
                result = classify_one(client, text)
                writer.writerow(
                    {
                        "filename": filename,
                        "county": county,
                        "announcement_type": result["announcement_type"],
                        "crime_type": result["crime_type"],
                    }
                )
                csvfile.flush()
                status = f"[{result['announcement_type']}]"
                if result["crime_type"]:
                    status += f" → {result['crime_type']}"
                print(f"  [{i}/{len(remaining)}] {county}/{filename}  {status}")
            except Exception as e:
                print(f"  [{i}/{len(remaining)}] ERROR {county}/{filename}: {e}", file=sys.stderr)
                writer.writerow(
                    {
                        "filename": filename,
                        "county": county,
                        "announcement_type": "ERROR",
                        "crime_type": str(e)[:200],
                    }
                )
                csvfile.flush()

    print(f"\nDone. Results written to {output_path}")


if __name__ == "__main__":
    main()

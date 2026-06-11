#!/usr/bin/env python3
"""
classify_local.py — Classify prosecutor press releases locally using Ollama.

Requires ollama running with llama3.2:
  brew services start ollama
  ollama pull llama3.2

Usage:
  python3 classify_local.py                     # classify all files
  python3 classify_local.py --sample 100        # random sample of 100
  python3 classify_local.py --county kings_ny   # one county only
  python3 classify_local.py --output myfile.csv # custom output path
  python3 classify_local.py --reclassify        # redo everything
"""

import argparse
import csv
import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import requests

# ─── Config ───────────────────────────────────────────────────────────────────

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2"

COUNTIES = [
    "los_angeles_ca", "cook_il", "harris_tx", "maricopa_az",
    "san_diego_ca", "orange_ca", "miami_dade_fl", "dallas_tx",
    "kings_ny", "riverside_ca",
]

MAX_PER_COUNTY = 1000

# ─── Taxonomy ─────────────────────────────────────────────────────────────────

ANNOUNCEMENT_TYPES = {
    "Conviction", "Arraignment", "Indictment", "Sentencing", "Arrest",
    "Charges Filed", "Office / Policy Announcement", "Award / Recognition",
    "Media Advisory", "Community / Prevention", "Statement",
    "Scam Alert / Public Warning", "Other",
}

CRIME_TYPES = {
    "Violent (murder / assault / robbery / weapons)",
    "Sex crimes",
    "White collar / Financial (fraud / embezzlement / tax evasion / deed theft)",
    "Drug crimes",
    "Human trafficking",
    "Child crimes (CSAM / child abuse)",
    "Property crimes",
    "Public corruption",
    "Other",
}

CRIMINAL_ANNOUNCEMENT_TYPES = {
    "Conviction", "Arraignment", "Indictment", "Sentencing", "Arrest", "Charges Filed",
}

SYSTEM_PROMPT = """You are a legal document classifier. You will be given the headline and opening paragraph of a press release from a prosecutor's or district attorney's office.

Classify it into exactly one announcement type from this list (use the exact label):
  - Conviction
  - Arraignment
  - Indictment
  - Sentencing
  - Arrest
  - Charges Filed
  - Office / Policy Announcement
  - Award / Recognition
  - Media Advisory
  - Community / Prevention
  - Statement
  - Scam Alert / Public Warning
  - Other

If and ONLY IF the announcement type is one of: Conviction, Arraignment, Indictment, Sentencing, Arrest, or Charges Filed — also classify the primary crime type from this list (use the exact label):
  - Violent (murder / assault / robbery / weapons)
  - Sex crimes
  - White collar / Financial (fraud / embezzlement / tax evasion / deed theft)
  - Drug crimes
  - Human trafficking
  - Child crimes (CSAM / child abuse)
  - Property crimes
  - Public corruption
  - Other

For ALL other announcement types, crime_type MUST be null. Do not set crime_type for Office / Policy Announcement, Media Advisory, Community / Prevention, Statement, Award / Recognition, or Scam Alert / Public Warning.

You MUST use only the exact labels listed above — no variations, no new categories.

Respond with JSON only — no explanation. Example:
{"announcement_type": "Sentencing", "crime_type": "Violent (murder / assault / robbery / weapons)"}"""

# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_files(county: Optional[str] = None) -> list[tuple[str, str]]:
    text_root = Path("text")
    results = []
    for c in ([county] if county else COUNTIES):
        d = text_root / c
        if not d.exists():
            print(f"  Warning: {d} not found, skipping", file=sys.stderr)
            continue
        files = [(c, str(f)) for f in sorted(d.glob("*.txt"))]
        if len(files) > MAX_PER_COUNTY:
            files = random.sample(files, MAX_PER_COUNTY)
            print(f"  {c}: capped at {MAX_PER_COUNTY} of {len(files) + MAX_PER_COUNTY} files")
        results.extend(files)
    return results


def extract_text_from_pdf_bytes(path: str) -> str:
    """Use pdftotext to extract text from a file that contains raw PDF bytes."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=Path(path).read_bytes(),
            capture_output=True,
            timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        pass
    return ""


def extract_lede(path: str, max_chars: int = 600) -> str:
    """Extract headline + first substantive paragraph — enough to classify."""
    try:
        raw_bytes = Path(path).read_bytes()
    except Exception as e:
        return f"[Error reading file: {e}]"

    # Detect raw PDF content stored in .txt files (Dallas)
    if raw_bytes[:4] == b"%PDF":
        text = extract_text_from_pdf_bytes(path)
        if not text:
            return "[PDF extraction failed]"
    else:
        text = raw_bytes.decode("utf-8", errors="replace").strip()

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return "[empty file]"

    # Strategy: find the first substantive paragraph (>= 80 chars) that comes
    # after any top-of-page boilerplate marker. This handles pages where nav/
    # sidebar text appears before and after the real content.
    EARLY_BOILERPLATE = {
        "skip to main content",
        "skip to content",
    }

    def is_short_boilerplate(line: str) -> bool:
        """Short lines that are nav/UI elements, not content."""
        l = line.lower().strip()
        # Breadcrumb nav
        if " > " in line and len(line) < 120:
            return True
        # Common nav words alone on a line
        if l in ("home", "back", "view all", "more news", "news releases",
                  "press release", "newsroom", "contact us", "find your case",
                  "resources", "careers", "about us"):
            return True
        return False

    # Find if early boilerplate exists (signals a nav-heavy page)
    has_early_bp = any(line.lower().strip() in EARLY_BOILERPLATE for line in lines[:10])

    if has_early_bp:
        # Find the first paragraph-like line: long AND contains sentence punctuation
        # (commas or periods mid-line). This skips nav headlines which lack punctuation.
        start_idx = 0
        for i, line in enumerate(lines):
            if len(line) >= 80 and (',' in line or '. ' in line or line.endswith('.')):
                start_idx = i
                break
    else:
        start_idx = 0

    # Grab headline + first substantive paragraphs starting from start_idx
    lede_lines = []
    chars = 0
    for line in lines[start_idx:]:
        if is_short_boilerplate(line) and lede_lines:
            break  # stop if we hit footer boilerplate after collecting content
        if len(line) < 15 and lede_lines:
            continue
        lede_lines.append(line)
        chars += len(line)
        if chars >= max_chars or len(lede_lines) >= 6:
            break

    # Fallback: if nothing collected, grab first substantive lines from top
    if not lede_lines:
        for line in lines:
            if len(line) < 15:
                continue
            lede_lines.append(line)
            chars += len(line)
            if chars >= max_chars or len(lede_lines) >= 6:
                break

    return "\n".join(lede_lines)


def classify_one(text: str) -> dict:
    """Send text to Ollama and return classification dict."""
    payload = {
        "model": OLLAMA_MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": text,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=30)
    resp.raise_for_status()
    raw = resp.json()["response"].strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    data = json.loads(raw)
    announcement_type = data.get("announcement_type", "Other")
    if announcement_type not in ANNOUNCEMENT_TYPES:
        announcement_type = "Other"

    crime_type = data.get("crime_type") or ""
    # Treat literal string "null" as empty (model sometimes returns it instead of JSON null)
    if crime_type.lower() in ("null", "none", "n/a"):
        crime_type = ""
    if announcement_type not in CRIMINAL_ANNOUNCEMENT_TYPES:
        crime_type = ""
    elif crime_type and crime_type not in CRIME_TYPES:
        crime_type = "Other"

    return {"announcement_type": announcement_type, "crime_type": crime_type}


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=0,
                        help="Classify a random sample of N files (0 = all)")
    parser.add_argument("--county", help="Restrict to one county key")
    parser.add_argument("--output", default="classifications_local.csv")
    parser.add_argument("--reclassify", action="store_true",
                        help="Ignore existing output and reclassify everything")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)

    # Check ollama is reachable
    try:
        requests.get("http://localhost:11434", timeout=3)
    except Exception:
        print("ERROR: Ollama is not running. Start it with: brew services start ollama")
        sys.exit(1)

    # Gather files
    all_files = get_files(args.county)
    print(f"Found {len(all_files)} files")

    if args.sample:
        all_files = random.sample(all_files, min(args.sample, len(all_files)))
        print(f"Sampled {len(all_files)} files")

    # Resume logic — skip already successfully classified rows
    done: set[str] = set()
    write_header = True
    if not args.reclassify and Path(args.output).exists():
        with open(args.output, newline="") as f:
            good_rows = [r for r in csv.DictReader(f)
                         if r.get("announcement_type", "") not in ("", "ERROR")]
        for row in good_rows:
            done.add(row["filename"])
        # Rewrite without error rows so they get retried
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer2 = csv.DictWriter(f, fieldnames=["filename", "county", "announcement_type", "crime_type"])
            writer2.writeheader()
            writer2.writerows(good_rows)
        write_header = False
        print(f"Resuming — {len(done)} already classified")

    remaining = [(c, p) for c, p in all_files if Path(p).name not in done]
    print(f"{len(remaining)} files to classify\n")

    if not remaining:
        print("Nothing to do.")
        return

    # Classify and write
    with open(args.output, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=["filename", "county", "announcement_type", "crime_type"],
        )
        if write_header:
            writer.writeheader()

        for i, (county, path) in enumerate(remaining, 1):
            filename = Path(path).name
            text = extract_lede(path)
            try:
                result = classify_one(text)
                writer.writerow({
                    "filename": filename,
                    "county": county,
                    "announcement_type": result["announcement_type"],
                    "crime_type": result["crime_type"],
                })
                csvfile.flush()
                status = f"[{result['announcement_type']}]"
                if result["crime_type"]:
                    status += f" → {result['crime_type']}"
                print(f"  [{i}/{len(remaining)}] {county}/{filename}  {status}")
            except Exception as e:
                print(f"  [{i}/{len(remaining)}] ERROR {county}/{filename}: {e}",
                      file=sys.stderr)
                writer.writerow({
                    "filename": filename,
                    "county": county,
                    "announcement_type": "ERROR",
                    "crime_type": str(e)[:200],
                })
                csvfile.flush()

    print(f"\nDone. Results written to {args.output}")


if __name__ == "__main__":
    main()

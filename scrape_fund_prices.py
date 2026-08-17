#!/usr/bin/env python3
"""
App 1 data pipeline: scrapes Maybank Singapore's public Unit Trust
Price List (a static, server-rendered reference page updated daily)
and matches the results against your target fund list.

Output: data/funds.json -- consumed by App 2 or any downstream app.

Usage:
    python scrape_fund_prices.py --targets hsbc_open_funds.csv --out data/funds.json

Designed to run on a schedule (e.g. GitHub Actions cron, every 12h).
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://sslsecure.maybank.com.sg/scripts/mbb_ut_pricelist.jsp"
USER_AGENT = "Mozilla/5.0 (compatible; FundTrackerBot/1.0; +personal-project)"

# Manager names as they appear as section headers on the page.
# Used to detect section boundaries; anything not in this set (and not
# a "Name of Funds..." column header) is treated as a fund data row.
KNOWN_MANAGERS = {
    "ABERDEEN ASSET MANAGEMENT ASIA LIMITED", "ALLIANCEBERNSTEIN INVESTMENTS",
    "ALLIANZ GLOBAL INVESTORS SINGAPORE LIMITED", "AMANAH MUTUAL BERHAD",
    "AMUNDI ASSET MANAGEMENT (S) LTD", "BLACKROCK",
    "BNP PARIBAS ASSET MANAGEMENT SINGAPORE LTD", "BNY MELLON GLOBAL MANAGEMENT LIMITED",
    "DEUTSCHE ASSET MANAGEMENT (ASIA) LTD", "EASTSPRING INVESTMENTS (S) LTD",
    "FIL INVESTMENT MANAGEMENT (S) LTD",
    "FIRST SENTIER INVESTORS (HONG KONG) LIMITED (HKD)",
    "FIRST SENTIER INVESTORS (HONG KONG) LIMITED (USD)",
    "FIRST SENTIER INVESTORS (SINGAPORE)", "FULLERTON FUND MANAGEMENT COMPANY LTD",
    "GOLDMAN SACHS ASSET MANAGEMENT",
    "HSBC GLOBAL ASSET MANAGEMENT (SINGAPORE) LIMITED",
    "IFAST FINANCIAL PTE LTD", "IFAST FINANCIAL PTE LTD (USD)",
    "JANUS HENDERSON INVESTORS", "JPMORGAN ASSET MANAGEMENT (S) LTD",
    "LEGG MASON ASSET MANAGEMENT (S) PTE LTD", "LIONGLOBAL INVESTORS LIMITED",
    "MAYBANK ASSET MANAGEMENT", "MAYBANK ASSET MANAGEMENT SDN BHD",
    "MANDIRI INVESTMENT MANAGEMENT PTE. LTD.",
    "MANULIFE INVESTMENT MANAGEMENT (SINGAPORE) PTE. LTD.",
    "NATIXIS GLOBAL ASSET MANAGEMENT", "NEUBERGER BERMAN", "PIMCO ASIA PTE LTD",
    "PINEBRIDGE INVESTMENTS SINGAPORE LIMITED",
    "SCHRODER INVESTMENT MANAGEMENT (S) LTD", "TEMPLETON ASSET MANAGEMENT LTD",
    "THREADNEEDLE INVESTMENTS SINGAPORE (PTE.) LIMITED",
    "UBS GLOBAL ASSET MANAGEMENT (S) LTD",
}

CURRENCY_MAP = {"US$": "USD", "S$": "SGD"}

FUND_LINE_RE = re.compile(
    r"^(?P<name>.+?)\s+"
    r"(?P<currency>US\$|S\$|EUR|GBP|AUD|HKD|CNH|JPY|MYR|IDR|CHF|NZD)"
    r"(?P<price>[\d,]+\.\d+)\s*-\s*"
    r"(?P<date>\d{2}\s+\w{3}\s+\d{4})\s*$"
)


def fetch_page(url: str = SOURCE_URL, timeout: int = 30) -> str:
    """Fetch the raw HTML. Raises on non-200 so the caller/cron job fails loudly."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def extract_lines(html: str) -> list[str]:
    """
    Convert the page to one text line per visual row. BeautifulSoup's
    get_text(separator='\\n') mirrors how the page reads visually,
    which is what the tested regex below expects.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_funds(lines: list[str]) -> list[dict]:
    """Walk the line stream, tracking the current manager section, and
    pull out every fund price row via the tested regex pattern."""
    results = []
    current_manager = None
    unparsed = []

    for line in lines:
        if line in KNOWN_MANAGERS:
            current_manager = line
            continue
        if line.startswith("Name of Funds"):
            continue  # column header row
        if line.startswith("Please Select") or line == "Top" or line.startswith("Refer to"):
            continue  # dropdown placeholder / nav noise

        m = FUND_LINE_RE.match(line)
        if m:
            raw_ccy = m.group("currency")
            results.append({
                "manager": current_manager,
                "fund_name": m.group("name").strip(),
                "currency": CURRENCY_MAP.get(raw_ccy, raw_ccy),
                "price": float(m.group("price").replace(",", "")),
                "price_date": m.group("date"),
            })
        # Anything else (marketing text, disclaimers) is silently
        # skipped -- only lines matching the fund-row pattern are kept.

    return results


def load_target_list(csv_path: str) -> list[dict]:
    """Load your canonical fund list (name + manager) to match against."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def normalize(name: str) -> str:
    """Loose normalization so 'Allianz Income and Growth AMi3 (H2-SGD)'
    and 'ALLIANZ INCOME AND GROWTH - CLASS AMi3 DIS (H2-SGD)' compare
    fairly: uppercase, strip punctuation noise, collapse whitespace."""
    name = name.upper()
    name = re.sub(r"[-–,]", " ", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def best_match(target_name: str, scraped: list[dict], threshold: float = 0.55):
    """Fuzzy-match one target fund name against all scraped rows using
    stdlib difflib (no extra dependency). Returns the best match dict
    plus its score, or None if nothing clears the threshold."""
    target_norm = normalize(target_name)
    best, best_score = None, 0.0
    for row in scraped:
        score = SequenceMatcher(None, target_norm, normalize(row["fund_name"])).ratio()
        if score > best_score:
            best, best_score = row, score
    if best_score >= threshold:
        return best, best_score
    return None, best_score


HIGH_CONFIDENCE = 0.85  # below this, share-class mismatches (SGD vs USD,
                         # hedged vs unhedged) are common enough that the
                         # match should be reviewed, not trusted blindly.


def build_output(targets: list[dict], scraped: list[dict]) -> dict:
    matched, unmatched = [], []
    for t in targets:
        row, score = best_match(t["fund_name"], scraped)
        if row:
            matched.append({
                "target_fund_name": t["fund_name"],
                "target_manager": t.get("fund_manager", ""),
                "matched_fund_name": row["fund_name"],
                "manager": row["manager"],
                "currency": row["currency"],
                "price": row["price"],
                "price_date": row["price_date"],
                "match_confidence": round(score, 3),
                "needs_review": score < HIGH_CONFIDENCE,
            })
        else:
            unmatched.append({
                "target_fund_name": t["fund_name"],
                "target_manager": t.get("fund_manager", ""),
                "best_score": round(score, 3),
            })

    return {
        "source": SOURCE_URL,
        "last_scraped": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "funds": matched,
        "unmatched": unmatched,  # review these -- likely need a manual name-alias fix or a different source
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--targets", required=True, help="CSV of target funds (fund_name, fund_manager)")
    ap.add_argument("--out", default="data/funds.json", help="Output JSON path")
    ap.add_argument("--min-expected-rows", type=int, default=500,
                     help="Sanity check: fail if scrape yields fewer rows than this (page structure may have changed)")
    args = ap.parse_args()

    print(f"Fetching {SOURCE_URL} ...")
    html = fetch_page()
    lines = extract_lines(html)
    scraped = parse_funds(lines)
    print(f"Parsed {len(scraped)} fund price rows from source.")

    if len(scraped) < args.min_expected_rows:
        print(
            f"WARNING: only {len(scraped)} rows parsed (expected >= {args.min_expected_rows}). "
            "The page structure may have changed -- check the regex/section-header logic "
            "before trusting this output.",
            file=sys.stderr,
        )

    targets = load_target_list(args.targets)
    output = build_output(targets, scraped)

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    needs_review = sum(1 for m in output["funds"] if m["needs_review"])
    print(f"Matched {output['matched_count']}/{len(targets)} target funds "
          f"({needs_review} flagged needs_review -- confidence < {HIGH_CONFIDENCE}).")
    print(f"Unmatched: {output['unmatched_count']} (see '{args.out}' -> unmatched[])")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()

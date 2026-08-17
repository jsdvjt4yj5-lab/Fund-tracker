#!/usr/bin/env python3
"""
App 1 data pipeline: scrapes Maybank Singapore's public Unit Trust
Price List (a static, server-rendered reference page updated daily)
and matches the results against your target fund list.

Output: data/funds.json -- consumed by App 2 or any downstream app.

Usage:
    python scrape_fund_prices.py --targets hsbc_open_funds.csv --out data/funds.json

Designed to run on a schedule (e.g. GitHub Actions cron, every 12h).

--- Change log ---
v2 (this version) -- fixed the real bug behind "0 matched / everything at
confidence 0.0": the page renders each fund as a genuine HTML <TR> with
FOUR SEPARATE <TD> cells (name, bid/NAV price, offer price, date). The old
parser flattened the page to plain text via BeautifulSoup's
get_text(separator='\\n') and tried to match a single-line regex like
"NAME  US$1.2300 - 14 Aug 2026" against it -- but get_text() puts each
<TD>'s text on its OWN line, so the name, price and date never appear
together on one line, and the regex could never match anything. Confirmed
via a real captured response: 0 rows via the old text-line approach,
1036+ rows via parsing the actual <TR>/<TD> structure directly (verified
against a real fetch from the live page on 17 Aug 2026).

parse_funds() now walks soup.find_all("tr") directly:
  - A manager section header is a <TR class="ttitle"> with one <TD>.
  - A fund row is any other <TR> with >= 4 <TD>s, where TD[1]'s text
    matches a currency+price pattern (e.g. "US$130.9200"). This also
    means the old hardcoded KNOWN_MANAGERS allowlist is gone -- manager
    names are read straight from the page's own section headers, so
    case/spelling drift there can no longer silently drop a whole
    section's funds the way it did before.

v1 changes (kept from the previous fix attempt, still relevant):
  - fetch_page() sends full browser-like headers, not just a custom bot
    User-Agent, since that's a common simple bot-filter trigger.
  - main() hard-fails (non-zero exit) if row count comes back too low,
    instead of silently writing an "empty but technically successful"
    output, and dumps the raw HTML to data/_debug_last_fetch.html so the
    actual response can be inspected. This is how the v2 bug above was
    actually found and fixed -- worth keeping for the next time
    Maybank changes their page structure.
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

# A real browser's header set -- clears simple User-Agent/header allowlist
# checks some bot filters use. (Turned out not to be the actual bug here --
# the fetch was always getting real content back -- but harmless to keep.)
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-SG,en;q=0.9",
    "Referer": "https://www.maybank2u.com.sg/",
    "Connection": "keep-alive",
}

CURRENCY_MAP = {"US$": "USD", "S$": "SGD"}

# Matches a price cell's full text, e.g. "US$130.9200", "S$3.5225", "CNH10.8697".
PRICE_RE = re.compile(
    r"^(?P<currency>US\$|S\$|EUR|GBP|AUD|HKD|CNH|JPY|MYR|IDR|CHF|NZD)"
    r"(?P<price>[\d,]+\.\d+)$"
)


def fetch_page(url: str = SOURCE_URL, timeout: int = 30) -> str:
    """Fetch the raw HTML. Raises on non-200 so the caller/cron job fails loudly."""
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_funds(html: str) -> list[dict]:
    """Walk the page's actual <TR>/<TD> table structure directly (not
    flattened text -- see module docstring change log for why). Each
    fund is a <TR> with the cell sequence: [name, bid/NAV price,
    offer price, date]. Manager section headers are <TR class="ttitle">
    with a single <TD> naming the manager -- read straight off the page,
    no hardcoded manager list needed."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    current_manager = None

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        if "ttitle" in (tr.get("class") or []):
            current_manager = tds[0].get_text(strip=True)
            continue

        first_text = tds[0].get_text(strip=True)
        if not first_text or first_text.startswith("Name of Funds"):
            continue  # blank row / column header row

        if len(tds) < 4:
            continue  # not a fund data row (e.g. the "Top" link row)

        price_text = tds[1].get_text(strip=True)
        date_text = tds[3].get_text(strip=True)

        m = PRICE_RE.match(price_text)
        if not m:
            continue  # doesn't look like a price cell -- skip defensively

        raw_ccy = m.group("currency")
        results.append({
            "manager": current_manager,
            "fund_name": first_text,
            "currency": CURRENCY_MAP.get(raw_ccy, raw_ccy),
            "price": float(m.group("price").replace(",", "")),
            "price_date": date_text,
        })

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
    ap.add_argument("--allow-low-rows", action="store_true",
                     help="Write output even if row count is below --min-expected-rows instead of failing "
                          "(useful for local debugging; do NOT use this in the scheduled workflow)")
    args = ap.parse_args()

    import os

    print(f"Fetching {SOURCE_URL} ...")
    try:
        html = fetch_page()
    except requests.RequestException as e:
        print(f"FATAL: request to {SOURCE_URL} failed: {e}", file=sys.stderr)
        sys.exit(1)

    scraped = parse_funds(html)
    print(f"Parsed {len(scraped)} fund price rows from source.")

    if len(scraped) < args.min_expected_rows:
        debug_path = os.path.join(os.path.dirname(args.out) or ".", "_debug_last_fetch.html")
        os.makedirs(os.path.dirname(debug_path) or ".", exist_ok=True)
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(html)
        msg = (
            f"Only {len(scraped)} rows parsed (expected >= {args.min_expected_rows}). "
            f"Raw response saved to {debug_path} for inspection -- open it and check whether "
            f"it's the real price list (parsing bug) or a block/CAPTCHA/error page "
            f"(network or bot-filtering issue)."
        )
        if args.allow_low_rows:
            print(f"WARNING: {msg}", file=sys.stderr)
        else:
            print(f"FATAL: {msg}", file=sys.stderr)
            sys.exit(1)

    targets = load_target_list(args.targets)
    output = build_output(targets, scraped)

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

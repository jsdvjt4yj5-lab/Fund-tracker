# App 1 — Fund Price Scraper

Scrapes Maybank Singapore's public [Unit Trust Price List](https://sslsecure.maybank.com.sg/scripts/mbb_ut_pricelist.jsp)
(static, server-rendered, updated daily) and matches results against
your target fund list (`targets.csv`, from the HSBC fund roster).

## Quick start

```bash
pip install -r requirements.txt
python scrape_fund_prices.py --targets targets.csv --out data/funds.json
```

## Automated 12h refresh (GitHub Actions)

`.github/workflows/scrape.yml` runs the scraper every 12 hours and commits
`data/funds.json` back to the repo. Enable **GitHub Pages** on this repo
(serving from the default branch) and App 2 can fetch the file at:

```
https://<your-username>.github.io/<repo>/data/funds.json
```

## Output shape

```json
{
  "source": "https://sslsecure.maybank.com.sg/scripts/mbb_ut_pricelist.jsp",
  "last_scraped": "2026-08-17T12:00:00+00:00",
  "matched_count": 58,
  "unmatched_count": 12,
  "funds": [
    {
      "target_fund_name": "Amundi Funds - Cash USD A2 USD (C)",
      "target_manager": "Amundi",
      "matched_fund_name": "AMUNDI FUNDS CASH USD - A2 USD (C)",
      "manager": "AMUNDI ASSET MANAGEMENT (S) LTD",
      "currency": "USD",
      "price": 130.92,
      "price_date": "14 Aug 2026",
      "match_confidence": 1.0,
      "needs_review": false
    }
  ],
  "unmatched": [
    { "target_fund_name": "Fundsmith Equity Fund R EUR Acc", "target_manager": "Fundsmith", "best_score": 0.52 }
  ]
}
```

## ⚠️ Known limitations — read before wiring this into App 2

1. **Coverage gap.** Maybank's page covers ~30 fund managers. Missing from
   your target list: **Ascend Asia AM, Fundsmith, Mirova, PGIM, Pictet,
   Robeco, UOBAM** (the two "United..." funds). These will always land in
   `unmatched[]` — you'll need a separate source for them (their own
   manager sites, per the earlier research in this project).

2. **Fuzzy matching is not exact.** Fund names differ in formatting between
   HSBC's list and Maybank's page (e.g. "US Bond A2 SGD Hgd" vs "US BOND
   A2 SGD-H"). The script uses `difflib` similarity scoring to bridge
   that gap. Any match below **0.85 confidence** is flagged
   `"needs_review": true` — **do not surface these to end users
   unreviewed**. Share classes that differ by currency or hedging (SGD
   vs USD, hedged vs unhedged) are the main failure mode: if the exact
   share class isn't present in the source, the matcher can pick the
   nearest-sounding wrong one with deceptively high confidence.

3. **"Indicative" pricing.** Maybank's own disclaimer states prices are
   "indicative...for your reference only," not official NAV. Fine for a
   client-facing display fund tracker; not something to build trade
   execution on.

4. **Sanity-check threshold.** The script warns (but doesn't fail) if it
   parses fewer than 500 rows total — a sign the page's HTML structure
   changed and the scraper needs a look. Worth wiring this into your
   monitoring/alerting once this runs unattended.

5. **Untested against the live page.** This was built and validated
   against real captured excerpts of the page's content, but the parser
   has not yet been run against a live fetch end-to-end. Run it manually
   once and check `data/funds.json` before trusting the scheduled job.

## Recommended next step

Run once locally, inspect `unmatched[]` and any `needs_review: true`
entries, and adjust `targets.csv` naming or add manual overrides for
persistent mismatches before turning on the scheduled workflow.

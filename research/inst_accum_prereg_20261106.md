# Pre-registration: institutional-accumulation test (2026-11-06)

Written before any shareholding data was downloaded. Rules are not changed after seeing results.

**Question.** Do stocks where mutual funds raised their stake in a quarter outperform over the next 3–6 months?

## Data
- NSE quarterly shareholding-pattern XBRL from `corporate-share-holdings-master`, quarters Sep-2021 to Sep-2026.
- MF% = the "Mutual Funds / UTI" category total: `MutualFundsOrUTI_ContextI` (fraction, x100) or `MutualFundsOrUtiI` (percent).
- FPI% = the "Institutions (Foreign)" total. A delta counts only if both quarters use the same XBRL key, because the format change mixes FPI-only and all-foreign definitions.
- Prices come from `delivery_cache/` (NSE bhavcopy), split-adjusted with `strategy_paper.load` (fixed 2026-11-06). ETFs are excluded.

## Point-in-time rules
- **Event date** = the filing's `broadcastDate`. **Entry** = the close of the first trading session strictly after that date.
- **Universe** = top 300 by 60-session median turnover on the entry date. That means a 662-symbol union is downloaded, and each event is kept only if the stock was in the top 300 on that day.
- **Signal** = ΔMF = MF%(q) − MF%(q−1), in percentage points. Deltas with |Δ| > 20 pts are dropped as data errors or mergers.

## Measurement
- **Excess return** = the stock's return over the window minus the equal-weight return of the point-in-time universe over the same window, entry to entry+h sessions. h = 126 (primary) and 63 (secondary).
- **Cohort** = all events for one shareholding quarter. Within each cohort, stocks are ranked by ΔMF into quintiles: Q5 = largest increase, Q1 = largest decrease.
- Cohorts with fewer than 25 events are dropped. This was added before any results existed, after a 3-symbol smoke test.
- **Cohort statistic** = the mean excess of Q5 minus 0.25% cost. Q5−Q1 spread is also reported.

## PASS (primary: Q5, h = 126) — all must hold
1. The mean cohort Q5 excess is greater than 0.
2. The t-stat over cohort means is at least 2.5. This is above the usual 2 because 126-session windows overlap across quarterly cohorts.
3. The mean is positive in both halves (first versus second half of cohorts by date).
4. At least 60% of cohorts are positive.
5. The mean Q5−Q1 spread is greater than 0.

## Secondary (reported, not used for PASS)
- h = 63.
- ΔFPI quintiles.
- An absolute rule: ΔMF ≥ +1.0 pt.
- Excess by calendar year.

## Known limitations
- Only about 5 years and 20 cohorts.
- The API is keyed by the current symbol, so delisted or renamed stocks are missing (survivorship). This biases toward a PASS, so a PASS needs extra caution.

## Data-cleaning note (2026-11-09, before any results)
Some 2025 XBRL filings store the percentage x100 (DATAPATTNS Jun-2025 MF = 672 for 6.72%). Any MF/FPI/DII value above 100 is divided by 100 before deltas are computed. Found while reading one stock's history, not from test output.

## Result (2026-11-09, run once on the full 662-symbol download)
5,462 events, 556 symbols, 18 qualifying cohorts at h=126.
- **Primary (ΔMF Q5, h=126): FAIL.** Q5 net +1.04%, t=1.34 (needs ≥2.5), halves −0.33 / +2.41 (first half negative), 61% cohorts positive, Q5−Q1 +2.40%.
- Secondary h=63: Q5 net +0.98%, t=2.49, halves −0.13 / +1.98, 79% positive.
- ΔFPI Q5 h=126: −1.88% (t=−2.18). Stocks where foreign funds raised stake underperformed.
- Absolute ΔMF ≥ +1pt: +0.54% at 63d, −0.29% at 126d.
Survivorship bias favours a PASS, so the true effect is likely smaller than shown.

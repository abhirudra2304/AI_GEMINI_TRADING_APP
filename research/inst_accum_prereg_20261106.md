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

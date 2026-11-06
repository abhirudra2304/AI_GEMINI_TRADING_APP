# BTST / SWING re-entry rule (pre-registered 2026-11-06)

BTST and SWING stay paper-only. The engine keeps logging signals and the nightly job scores them (`signals.db` → `outcomes`). A strategy earns its way back to real money only if **all** of the following hold on resolved outcomes whose signal date is after 2026-11-06:

1. **n ≥ 60** resolved trades (WIN / LOSS / TIME_STOP / GAP_THROUGH_STOP).
2. **Mean `adj_return_pct` > 0**, net of the friction already in that column.
3. **Win rate above break-even.** Break-even = |mean loss| / (mean win + |mean loss|), computed from the same trades.
4. **Both halves positive**: mean `adj_return_pct` > 0 in the first and second half of those trades by signal date.
5. **Not one stock**: removing the single best symbol still leaves the mean > 0.

**Sunset, agreed with the user 2026-11-06:** if neither strategy has EARNED BACK by about 2027-02-06 (3 months), stop running the BTST/SWING scans. Every idea and outcome is in `journal/btst_swing_ideas.csv` (rewritten nightly from signals.db) and `journal/eod_ideas.csv` (appended each EOD; this replaced the daily PDFs).

`journal_review.py` checks this automatically and prints EARNED BACK or NOT YET for each strategy. The rule is not changed after seeing results.

Baseline when this was written:
- **BTST:** about 1–5% wins a month from May to Nov 2026; mean adj return −0.09% to −1.0% a month.
- **SWING:** fell from about 20% wins (May–Jun) to 1–8% (Jul–Nov).

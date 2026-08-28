# Daily Operating Runbook

How to run this system day to day, given the number of tools accumulated
(2026-08-07 to 2026-08-10 sessions). See `SESSION_NOTES.md` for the history/
decisions behind each tool; this file is just "what to run, when."

## Current status: automation is NOT reliable — run manually until fixed

`TradingApp_Prewarm` (14:25) and `TradingApp_EOD` (15:15) have failed in
**three different ways across three sessions** (Aug 3: missed entirely;
Aug 7: likely machine sleep; Aug 10: fired on time, killed ~17 min in).
**Do not assume they ran.** Check before trusting any "fast path" that
depends on a same-day report:

```bash
python -c "from data_broker import DataBroker" # sanity: venv works
```
```powershell
Get-ScheduledTaskInfo -TaskName "TradingApp_Prewarm" | Select LastRunTime,LastTaskResult
Get-ScheduledTaskInfo -TaskName "TradingApp_EOD" | Select LastRunTime,LastTaskResult
```
`LastTaskResult` should be `0`. Anything else (commonly `3221225786` =
`STATUS_CONTROL_C_EXIT`) means it didn't complete — treat today's cached
report as untrustworthy and re-run manually.

**Until the scheduling issue is root-caused and fixed (tracked in
`SESSION_NOTES.md` P1), the practical daily routine is: ask Claude to run
the steps below manually.** That's not a workaround to feel bad about —
it's the correct call until the automation proves itself again.

---

## Daily (every trading day)

1. **Anytime after 09:15** — check for live signals, no gate:
   ```bash
   python main.py momentum
   ```
2. **~15:10–15:15, before the 15:20 decision cutoff** — the actual decision tool:
   ```bash
   python main.py stockscan
   ```
   Chains: scan -> fitness filter (BTST/SWING-eligible only) -> proven-tier
   filter (reads the weekly scorecard) -> extension check (fresh vs chased)
   -> catalyst cross-reference. Reuses today's report if one exists
   (~30s); only re-scans from scratch (~20-28 min) if nothing from today
   is on disk yet.
3. **After 15:30 close** — the full consolidated report:
   ```bash
   python main.py eod
   ```
   Runs BTST + SWING + EMFB, saves a PDF. If run post-close, the app
   itself will warn the picks may be stale (Phase 2 needs live intraday
   candles) - that's an honest built-in check, not a bug. Output rows
   are tagged with a `Data_Stale` flag (from `data_broker.py`'s staleness
   detection) and an informational-only `Institutional_Score` (real NSE
   bhavcopy delivery data, not wired into ranking - see
   `institutional_flow.py`). As of 2026-08-25, `eod` also (both alert-only,
   never block the report if they fail): appends today's picks to a shared
   Google Sheet for trend history (`sheets_logger.py`) and checks
   `watchlist_positions.yaml` for a momentum-drop alert on held positions
   (`momentum_alert.py`).
4. **Anytime, ad hoc:**
   ```bash
   python main.py trigger-status emfb      # is an existing signal still valid?
   python main.py trigger-status momentum
   python main.py news                     # fresh NSE filings vs latest scan
   python main.py history SYMBOL           # one symbol's score trail across scans
   python main.py watchlist                # new/dropped/continuing vs last scan
   python main.py surge                    # market-wide volume+price surge scan (whole bhavcopy, not just the 219-name universe)
   python main.py gapscan                  # today's open vs prior close, curated universe; run near 09:15 (new 2026-08-25, not yet folded into stockscan - see SESSION_NOTES.md)
   python main.py reliability              # per-symbol win rate/avg return from resolved BTST/SWING trade history; STRONG/UNRELIABLE flags (new 2026-08-25, IS wired into stockscan's shortlist as an informational flag)
   ```
   `surge` produces a `NEEDS_INVESTIGATION` list (real surge, no NSE filing explaining it) - a Python script can't web-search, so **Claude must personally web-search those flagged names** after running it, same pattern as the 2026-08-11 MCX/Jefferies find. Don't just print the raw table and stop there.

## Weekly (once a week — these are slow-changing; everything else depends on them)

```bash
python main.py fitness      # re-tags universe: BTST_ELIGIBLE / SWING_ELIGIBLE / TOO_SLOW / TOO_ILLIQUID
python main.py expand       # external F&O candidates + swap suggestions vs current universe
python main.py scorecard emfb       # real win rate by Confidence tier
python main.py scorecard momentum   # same, once momentum has enough resolved history
```
Run these Monday morning before market open, or over the weekend. `stockscan`
warns if these are missing/stale, but will still run — just with weaker
filtering (no proven-tier ranking, no fitness restriction).

## Not yet automatic — deliberately gated, do not skip ahead

- **`discovery.py` (V1.0 BTST/SWING) does not yet use the bhavcopy daily-candle
  source** that `momentum_scanner.py` uses — still on the throttled per-symbol
  Angel path. Gated behind `validate_bhavcopy.py` showing clean across
  several sessions (day 1: clean, 0% discrepancy). Re-run the validator each
  session; do not bring the swap to `discovery.py` until it's proven over
  multiple days.

**RESOLVED:** universe fitness gating is no longer advisory-only —
`discovery.py` now actually restricts BTST/GAP/INTRADAY discovery to
`BTST_ELIGIBLE` names (reads `universe_fitness.csv`'s `Primary_Tag`/`All_Tags`),
confirmed live in code as of 2026-08-25. SWING intentionally still scans the
full universe (to preserve compounder alpha), not a gap.

## One-command mental model

If you only remember one thing: **`python main.py stockscan` around 15:10
is the command that matters for a same-day decision.** Everything else is
either feeding it (weekly precompute) or checking it after the fact
(trigger-status, news, history).

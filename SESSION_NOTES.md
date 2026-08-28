# Session Notes — 2026-08-07 (updated 2026-08-11)

> **2026-08-11 update — both scheduled tasks completed successfully for the first
> time. NOT yet confirmed as a fix - one data point, and it complicates the
> LogonType theory.** `TradingApp_Prewarm` (14:25:01 -> 15:19:26, 54min, slow but
> clean, `LastTaskResult: 0`) and `TradingApp_EOD` (15:15:00 -> 15:36:21, 21min,
> `LastTaskResult: 0`) both ran to completion with genuine "run finished" log
> markers - a first across this entire investigation.
>
> **Important wrinkle: `TradingApp_EOD` succeeded today while STILL on
> `LogonType: Interactive`** - the setting we identified as the likely root
> cause on Aug 10 was never actually changed for this task (user stopped
> troubleshooting before EOD's credential step). So today's clean run does NOT
> confirm the Interactive-logon-type theory was correct - it's equally
> consistent with "today just didn't have whatever session event (screen
> lock/sleep) caused the Aug 10 kill." `TradingApp_Prewarm` is still on `S4U`
> (the partial fix from Aug 10), so its success is ambiguous too - could be S4U
> holding, could just be a clean day.
>
> **Do not declare this fixed.** Needs several more consecutive clean days
> before trusting it. If either task fails again, that's strong evidence today
> was a fluke, not a fix. Keep checking `LastRunTime`/`LastTaskResult` each
> session per `DAILY_RUNBOOK.md`'s verification step - don't drop the manual
> workflow as a fallback yet.

> **2026-08-10 update — root cause found for the scheduled-task kill; fix attempted,
> in progress (not yet complete).** Checked the task's `Principal` via
> `Get-ScheduledTask`: `LogonType: Interactive` on both tasks. That's the likely
> root cause of the Aug 10 mid-run kill (and plausibly Aug 7's identical-timestamp
> mystery too, as one unified explanation) - "Interactive" ties the task to the
> actual desktop session, so a screen lock/session change during the ~20-min run
> can tear down the task's process. `ExecutionTimeLimit` (72h) and
> `MultipleInstances` (IgnoreNew) were ruled out first - not the cause.
>
> **Fix attempted, partial:** switching to "Run whether user is logged on or not"
> requires the account password stored with the task. User's Windows account is a
> Microsoft account (Gmail-linked) but resolves locally as `LOVELYLAPTOP\abhir`
> (confirmed via `whoami`) - the Task Scheduler prompt initially rejected the
> Microsoft-account email format; using `LOVELYLAPTOP\abhir` via "Change User or
> Group" -> "Check Names" resolved correctly. Password step didn't fully complete
> for either task this session:
> - **`TradingApp_Prewarm`: `LogonType` is now `S4U`** (changed from `Interactive`,
>   but not the full fix - `S4U` typically still needs an existing logon session,
>   may not survive full logoff/certain sleep states the way real `Password`
>   logon type would). **Needs a live scheduled run to confirm whether this alone
>   is enough, or whether the password step needs to be redone.**
> - **`TradingApp_EOD`: still `Interactive`, untouched.** Same fix needs to be
>   applied here too.
>
> **DECIDED, same day: fix deferred indefinitely, manual workflow is now the
> standing operating mode.** After the password reset, both the Task Scheduler
> GUI and a direct `Set-ScheduledTask -User ... -Password ...` PowerShell attempt
> still failed with a logon error (`HRESULT 0x8007052e` / "user name or password
> is incorrect") even with the confirmed-correct `LOVELYLAPTOP\abhir` username
> format. The isolating diagnostic (sign out, log in via password instead of PIN
> to confirm the password itself is valid before suspecting 2-Step
> Verification/app-passwords) was not completed - user chose to stop
> troubleshooting and rely on the manual workflow instead. **This is a deliberate,
> accepted decision, not a dropped thread** - do not re-raise it proactively.
> Current state, unchanged since: `TradingApp_Prewarm` = `LogonType: S4U`
> (still non-functional, `LastTaskResult` still failing), `TradingApp_EOD` =
> `LogonType: Interactive` (untouched, still failing). **If the user brings this
> up again in a future session**, the next concrete step is still the diagnostic
> above (verify password via lock-screen password login) before retrying
> `Set-ScheduledTask`, and the app-password route
> (account.microsoft.com/security -> Advanced security options -> App passwords)
> is the most likely fix if 2-Step Verification turns out to be on. **Until then,
> the daily workflow is 100% manual per `DAILY_RUNBOOK.md` - ask for `momentum`
> once earlier in the day, then `stockscan` at ~15:10. This works fine and loses
> no functionality, it's just not hands-off.**

> **2026-08-10 update — third distinct scheduled-task failure mode found; audited
> a full trading day.** Checked whether every scanner ran on schedule with
> meaningful results today. Verdict: **no scanner ran cleanly on schedule.**
> - `TradingApp_Prewarm` (14:25): fired ON TIME this time (unlike Aug 3/7), but
>   died ~17 min in (14:42) - only 6.2% through BTST's intraday fetch, never
>   reached SWING/EMFB/momentum, no "finished" log line, `LastTaskResult`
>   `STATUS_CONTROL_C_EXIT` (same code as the Aug 7 incident).
> - `TradingApp_EOD` (15:15): did not fire at all - `LastRunTime` still shows
>   Aug 7, no log file for today.
> - Everything with real results today (BTST/SWING/momentum discovery at 10:26,
>   momentum re-runs, EMFB check at 15:21) was **manually triggered**, not
>   scheduled.
> - **Found while auditing:** the BTST fast-execution result reported earlier
>   today (TVSMOTOR 72.3 top, all WEAK) was built on the discovery cache the
>   killed 14:25 prewarm left behind (`generated: 14:32:29`, now flagged
>   **invalid** by `DiscoveryCache.get_cache_metadata`) - i.e. built on a
>   possibly-incomplete intraday dataset, not a clean full cycle. **Treat that
>   result as unconfirmed** until re-run against a clean cache (market was
>   closed by the time this was discovered, so re-validation waits for the
>   next session).
> - **This is now the third distinct failure signature** across three
>   sessions: Aug 3 (missed entirely), Aug 7 (likely machine sleep, both tasks
>   silently missed), Aug 10 (fired on time, killed mid-run). Different causes
>   each time - this is a systemic automation reliability gap, not a single
>   fixable root cause. The "check Windows power settings" fix already flagged
>   addresses only the Aug 7 mode, not today's.

> **2026-08-10 update — bhavcopy daily-candle source, staged rollout.** Monday
> market-open cache refresh hit heavy Angel rate limiting (BTST: 210 RateLimit
> errors, SWING: 204) purely on per-symbol daily-candle fetches. **Root fix
> (option B over adding a second broker API):** NSE's public bhavcopy gives the
> whole market's daily OHLCV in one free, unauthenticated call per trading day -
> built `nse_daily_history.py` (persistent disk cache, `bhavcopy_cache/`) and
> wired it into `momentum_scanner.py`'s universe daily fetch (218/219 symbols
> covered, 1 broker fallback). Cold backfill: 76.6s for the whole 219-symbol
> universe x 290 days (vs per-symbol throttled broker calls); warm: 6.3s.
>
> **Full live momentum run confirmed:** zero rate-limit warnings anywhere in
> the run. Total wall-clock barely changed (23.3 min vs ~28 min) because
> momentum's runtime is actually dominated by intraday (hourly/15m/5m)
> broker fetches, not daily - bhavcopy has no intraday data, so that part is
> untouched. **The bigger unclaimed win is `discovery.py` (BTST/SWING)**,
> which is mostly daily-only and is exactly what produced this morning's 400+
> rate-limit errors - but that's V1.0 with a validated track record behind
> it, so per the plugin-architecture rule it's being staged, not swapped in
> immediately.
>
> **Staged plan (in progress, do not skip ahead):** built `validate_bhavcopy.py`
> - a repeatable reliability check (bhavcopy vs broker close prices, flags any
> >1% discrepancy). **Day-1 result (2026-08-10, 30-symbol sample): 252/252
> genuine comparisons OK, 0.000% max discrepancy.** Re-run this each session
> over the next several days; only bring the same swap to `discovery.py` once
> it's shown clean across multiple independent days. Do not shortcut this -
> `discovery.py` feeds BTST/SWING's live scoring directly.

What was built, found, and what remains. Everything this session is **plugin-only
and additive** — no changes to V1.0's live `main.py btst`/`swing` output unless a
P1 switch below is deliberately flipped.

> **Update (later same day) — live test + timing fix.** First live market-hours run
> of `stockscan` validated the whole pipeline (earnings verifier made **49 NSE
> fallbacks + caught 23 imminent-earnings vetoes** in one scan; extension correctly
> demoted chased names). It also exposed a timing bug: cold-cache `stockscan` took
> **28 min** (blew the 15:20 deadline) because it re-scanned the full universe.
> **Fixed:** `stock_scan.py` now reuses today's already-computed report (scans only as
> a fallback), and `scripts/prewarm_eod.ps1` now also runs `momentum` at 14:25 so a
> today-dated report exists by EOD. Result: **~31s instead of 28 min (55×)**, verified
> live. This also closes P2's "validate a live momentum run" item.

> **Update (same day, later) — scheduled tasks silently missed today; likely machine
> sleep.** `TradingApp_Prewarm` (14:25) and `TradingApp_EOD` (15:15) both silently
> failed to fire at their real trigger times today. Investigation: `LastRunTime` for
> both showed **17:52:56 - the identical timestamp** - with `LastTaskResult`
> `3221225786` (`STATUS_CONTROL_C_EXIT`, i.e. both got killed/interrupted). A manual
> cache-warm run started ~14:57 also took **~3 hours** (14:57->17:58) despite the API
> stats showing **0 retries, 0 cooldowns** - meaning it wasn't rate-limited, it simply
> didn't progress for hours. All three anomalies are consistent with **the machine
> sleeping/being suspended ~14:57-17:52**: Windows fires missed weekly triggers as a
> batch on wake (explaining the identical 17:52:56 timestamp for two unrelated tasks),
> and a foreground process would show the same wall-clock stall with no logged retries.
> Not proven from inside the app (no direct "system slept" log line found - Windows'
> `Microsoft-Windows-TaskScheduler/Operational` event log is disabled on this machine,
> `IsEnabled=False`, so there's no further forensic trail). **Consequence:** no EOD
> report was produced at all today; the 11:38 momentum report (from the live-test
> earlier that day) remained the freshest data through market close.
>
> **Live fallout, found and fixed same day:** two `main.py` processes were found
> running with **no arguments** (silently defaulting to `btst` mode, which blocks
> forever) - created 17:53:06/07, i.e. spawned in the collision when the missed tasks
> and/or manual runs all tried to launch around the same moment on wake. These were
> holding/contesting the instance lock and would have blocked Monday's 14:25/15:15
> triggers too if left running over the weekend. **Killed both; `main.pid` confirmed
> clear.** This is the *second* time this exact "stray no-args main.py blocking the
> lock" pattern has appeared in one day (see the two killed at 00:36 earlier).
>
> **Action item for the user (cannot be fixed from inside the app):** check Windows
> power settings - whether this machine is allowed to sleep during trading hours, and
> whether `TradingApp_Prewarm`/`TradingApp_EOD` have **"Wake the computer to run this
> task"** enabled (Task Scheduler -> task Properties -> Conditions tab). Until this is
> fixed, the entire automated pipeline (prewarm, EOD, and by extension `stockscan`'s
> fast path) is at risk of silently not running on any day the machine sleeps.

---

## ✅ ACHIEVED

### New capabilities (12 plugins, all verified against real data)
- **`earnings_verifier.py`** — NSE board-meetings fallback for Yahoo's earnings gaps
  (fixed a **33% "UNKNOWN" rate**). Wired into `emfb.py` and `momentum_scanner.py`.
- **`trigger_status.py`** — freshness check: FRESH / EXTENDED / FADED / STOPPED_OUT /
  HIT_TARGET. Command: `trigger-status`.
- **`symbol_history.py`** — cross-report memory: score/rank trail + new/dropped/
  continuing diff. Commands: `history`, `watchlist`.
- **`performance_tracker.py`** — real win/loss scorecard. Command: `scorecard`.
- **`signal_quality.py`** — relabels signals by *proven* win rate, not static score.
  Command: `actionable`.
- **`extension_score.py`** — overextension penalty (ATR/EMA20). Command: `extension`.
- **`momentum_scanner.py`** — always-on, non-regime-gated scan. Command: `momentum`.
- **`nse_delivery_feed.py`** — real delivery volume, replaced IAS's `0.0` stub.
- **`nse_announcements.py`** — catalyst scanner (order wins, acquisitions, ratings).
  Command: `news`.
- **`universe_fitness.py`** — short-term trading-fitness screener. Command: `fitness`.
- **`universe_expansion.py`** — external F&O add/replace sourcing. Command: `expand`.
- **`stock_scan.py`** — master command chaining it all. Command: `stockscan`.

### Key findings (real data)
- **Win rate by tier: High 92.3% (12W/1L), Medium 43.5%, Low 47.8%** — the edge is
  almost entirely in the High tier; Medium/Low are coin flips. (Only 82 of 463 signals
  resolved so far — directional, not yet statistically robust.)
- **45%** of the universe fails the Minervini trend gate even in a STRONG BULL regime.
- **34 current names unfit for short-term trading** — 16 TOO_SLOW (incl. RELIANCE,
  ICICIBANK, SBIN — great holds, dead charts) + 18 TOO_ILLIQUID.
- **75 of 77 external F&O candidates** score more trade-fit than the worst current names.
- IAS pipeline's **first successful full run** (205 scored, 18 actionable).

### Fixes & housekeeping
- Universe cleanup: `MCDOWELL-N`→`UNITDSPR`, removed dead `SGEL` (all 219 now resolve).
- Killed 2 stuck `btst` processes blocking the instance lock.
- Instrumented the silent `reports.db` write-failure (added visible `print()`).
- Saved **"stock scan" playbook to memory** (`project_stock_scan_playbook.md`).

### Baseline snapshots on disk
- `universe_fitness.csv` — 118 BTST_ELIGIBLE / 35 SWING / 28 marginal / 18 illiquid /
  16 slow / 4 no-data.
- `universe_expansion_candidates.csv` — 77 external F&O scored, 75 BTST_ELIGIBLE.
- `tier_performance.json` — the by-tier win rates that `actionable` reads.

---

## 🔧 NEEDS WORK (future sessions, prioritized)

### P1 — highest leverage
- **Bring bhavcopy daily-candle source to `discovery.py` (BTST/SWING), once validated.**
  See the 2026-08-10 update at top of file. `validate_bhavcopy.py` must show clean
  results across several more sessions before this V1.0 swap happens - re-run it each
  session (`python -c "from validate_bhavcopy import validate_symbols, print_report;
  print_report(validate_symbols([...symbols...], sample_days=10))"`). This is the fix
  that actually addresses the rate-limit incidents (momentum's own runtime is
  intraday-bound, not daily-bound, so the win there was rate-limit pressure, not speed).
- **Fix scheduled-task reliability - THE REAL BLOCKER, now confirmed systemic (3
  different failure modes across 3 sessions), not a single fixable cause.** Power
  settings ("Wake the computer to run this task") addresses the Aug 7 mode
  (machine sleep) but NOT the Aug 10 mode (task fired on time, then got killed
  mid-run - see the 2026-08-10 update at top of file). The Aug 10 kill needs its
  own investigation - check Task Scheduler's "Stop the task if it runs longer
  than" setting on both tasks (default is often short, e.g. 3 days is fine but
  some environments default much lower), and check for any other process/policy
  that could be sending a stop signal around the 15-20 min mark. Do not assume
  the power-settings fix alone resolves this - verify with a full clean
  scheduled run before trusting `stockscan`'s fast path again.
  **Confirmed same day (Aug 7):** a manual post-close
  `eod` run (18:11) completed fine but came back almost entirely WEAK - because it
  never got the real 15:15 intraday data the scheduled run would have captured. EMFB
  also produced zero signals since its 14:30-18:00 window had already closed. So this
  isn't just a missed convenience run - it's the difference between a real signal and
  a stale one. Until fixed, the whole automated pipeline (and `stockscan`'s fast path,
  which depends on a same-day report existing) is at risk on any day the machine sleeps.
  **Do this before Monday.**
- **Wire eligibility into the live scans.** `fitness`/`expand` are advisory only today —
  they don't change what `btst`/`swing` scan. Make BTST restrict to `BTST_ELIGIBLE`.
- **Schedule the weekly precompute.** `fitness`, `expand`, `scorecard` must run weekly to
  keep `stockscan` sharp — not yet in the scheduled `.ps1` tasks.
- **Execute the universe rebalance.** Swap candidates exist; actually editing `config.py`
  to add/replace names is a deliberate, user-owned decision, not yet done.

### P2 — trust & robustness
- **Scorecard sample is thin** — 92.3% High-tier rests on just 13 resolved signals.
  Re-run `scorecard` as more reports resolve; treat as directional until n grows.
- **IAS has zero track record.** Before trusting it over V1.0, point
  `performance_tracker.py` at IAS picks once it has a few weeks of history.
- **`reports.db` root cause still unknown.** Now instrumented — next live failure will be
  visible. Needs a real run to catch.
- ~~Validate a live `momentum` run during market hours~~ — **DONE 2026-08-07**: ran live,
  124 signals, pipeline verified; timing bug found and fixed (see top-of-file update).
- **`stockscan momentum` win-rate is INSUFFICIENT_DATA** — correct, not a bug: momentum's
  first report was produced today, so it has no track record yet. Run `scorecard momentum`
  once several momentum reports have accumulated; until then use `stockscan emfb` for the
  proven 92.3% High-tier ranking, or accept score-only ranking on momentum.

### P3 — depth & hardening
- **Tier-2 catalyst reads** — `news` only category-tags filings; judging *magnitude* needs
  the actual filings read on the shortlist (LLM-in-the-loop step).
- **Extend the real delivery feed to V1.0's own institutional metrics** (currently IAS-only).
- **No unit tests** on the 12 new modules (follow the `test_emfb.py` pattern).
- **Threshold validation** — extension bands (1.8%/8%), fitness cutoffs, materiality tiers
  are sensible defaults, not backtested against outcomes.
- **Big deferred effort:** wire IAS's regime-aware scoring into the live BTST/SWING path
  (not just standalone `python pipeline_runner.py`).

---

## Operational notes
- **Always run with the project venv:** `.venv\Scripts\python.exe` — system Python lacks
  `yfinance` and will crash on import.
- **Master command:** `python main.py stockscan [momentum|emfb]` — produces a decision
  shortlist before 15:20 IST. Weekly precompute (`fitness`/`scorecard`) must be fresh or
  the command warns and the shortlist is weaker.
- All new commands: `momentum`, `trigger-status`, `history`, `watchlist`, `scorecard`,
  `actionable`, `extension`, `news`, `fitness`, `expand`, `stockscan`.

"""Master 'STOCK SCAN' orchestrator - produces a decision-ready shortlist
before the 15:20 IST deadline (leaving 5-8 min to decide buy/not before the
15:30 close).

Chains the session's plugins in a timing-aware order. The critical insight:
the heavy full-universe steps (universe_fitness, universe_expansion,
performance scorecard) each cost a ~200-symbol API sweep and CANNOT fit in
the ~10-min pre-close window - so they are PRE-COMPUTED periodically (weekly
is fine; their outputs change slowly) and this daily path only READS their
cached outputs. The only heavy daily step is the scan itself, which the
14:25 prewarm task already warms the cache for.

Daily critical path (this file):
  1. Scan      - run_momentum_scan (always-on) or reuse today's fresh report.
  2. Fitness   - restrict to BTST/SWING-eligible names (reads
                 universe_fitness.csv; no API).
  3. Actionable- keep only proven-tier signals (reads tier_performance.json;
                 no API).
  4. Extension - flag overextended entries, on the SHORTLIST ONLY (~15 API
                 calls, not the whole universe) to stay inside the deadline.
  5. Catalyst  - cross-reference NSE announcements (one HTTP call).
  5b. Reliability - flag (never filter) shortlist names against real resolved
                 trade history (signals.db outcomes; no API - see
                 symbol_reliability.py). Added 2026-08-25.
  6. Present   - ranked decision shortlist.

Every phase is fail-soft: a phase that errors is skipped (logged) rather
than taking down the shortlist - hitting the 15:20 deadline with a partial
list beats missing it with a complete one. No edits to any V1.0 file.
"""
import logging
import time
from datetime import datetime
from typing import Optional

import pandas as pd

import config

logger = logging.getLogger(__name__)

# Weekly-refresh prerequisites this daily path reads but does not compute.
PRECOMPUTED_INPUTS = {
    'universe_fitness.csv': 'python main.py fitness',
    'tier_performance.json': 'python main.py scorecard',
}


def _phase(msg: str, t0: float) -> None:
    print(f"  [{time.time() - t0:5.1f}s] {msg}")


def _report_is_from_today(path: str) -> bool:
    """Report filenames are <type>_report_YYYYMMDD_HHMMSS.csv - true if that
    date is today (IST). This is what lets stockscan reuse the heavy scan
    the 14:25 prewarm / 15:15 eod task already ran, instead of re-scanning."""
    import re
    m = re.search(r'_(\d{8})_', path)
    if not m:
        return False
    return m.group(1) == datetime.now(config.MARKET_TZ).strftime('%Y%m%d')


def _get_scan_report(report_type: str, t0: float) -> Optional[pd.DataFrame]:
    """Fast path: reuse today's freshest report (produced by the scheduled
    prewarm/eod scan) - no re-scan, ~20s total, hits the 15:20 deadline.
    Only if no report exists for today does it run a fresh scan itself
    (the ~28-min cold path), so a standalone/ad-hoc invocation still works.

    Root cause this fixes (2026-08-07 live test): stockscan was always
    running its own full-universe momentum scan (~28 min cold), duplicating
    work the scheduled pipeline already does. Reusing today's report mirrors
    how eod already reuses the prewarmed EMFB cache."""
    from trigger_status import find_latest_report
    rt = report_type if report_type in ('emfb', 'momentum') else 'emfb'

    # Fast path - today's report already on disk.
    path = find_latest_report(rt)
    if path and _report_is_from_today(path):
        _phase(f"Scan: reusing today's fresh report {path} (no re-scan).", t0)
        return pd.read_csv(path)

    # No report from today - run the scan (slower fallback; momentum is
    # always-on, emfb is regime-gated so may still yield nothing).
    if rt == 'momentum':
        try:
            from momentum_scanner import run_momentum_scan
            df = run_momentum_scan(force_refresh=False)
            if df is not None and not df.empty:
                _phase(f"Scan: no report from today - ran momentum, {len(df)} signals.", t0)
                return df
        except Exception as e:
            logger.warning(f"momentum scan failed ({e}); falling back to latest report file.")

    # Final fallback - the most recent report even if stale (better a stale
    # shortlist to sanity-check than nothing).
    if path is None:
        _phase("Scan: no report available at all.", t0)
        return None
    _phase(f"Scan: no report from today; reusing latest {path} (STALE - verify).", t0)
    return pd.read_csv(path)


def _apply_fitness_filter(df: pd.DataFrame, t0: float) -> pd.DataFrame:
    """Restrict to names tagged BTST/SWING-eligible by universe_fitness.csv
    (short-term-tradeable vehicles). Skipped if the file is absent."""
    import os
    if not os.path.exists('universe_fitness.csv'):
        _phase("Fitness: universe_fitness.csv absent - skipping (run `python main.py fitness`).", t0)
        return df
    try:
        fit = pd.read_csv('universe_fitness.csv')
        eligible = set(fit[fit['Primary_Tag'].isin(['BTST_ELIGIBLE', 'SWING_ELIGIBLE'])]['Symbol'])
        before = len(df)
        out = df[df['Symbol'].isin(eligible)].copy()
        _phase(f"Fitness: {len(out)}/{before} signals are on trade-fit names.", t0)
        # If the filter wipes everything (mismatched universe), keep original.
        return out if not out.empty else df
    except Exception as e:
        logger.warning(f"Fitness filter failed ({e}); skipping.")
        return df


def _apply_actionability(df: pd.DataFrame, report_type: str, t0: float) -> pd.DataFrame:
    try:
        from signal_quality import annotate_actionability
        out = annotate_actionability(df, report_type)

        # 2026-08-21: bring scorecard staleness up into stockscan's own phase
        # output (not just the print already inside annotate_actionability) -
        # this is the actual "will the user actually see it" fix. A stale
        # scorecard degraded ranking quality silently before this.
        if out.attrs.get('scorecard_stale'):
            age_days = out.attrs.get('scorecard_age_days')
            if out.attrs.get('scorecard_missing'):
                _phase(f"⚠️ Actionable: NO scorecard for '{report_type}' - run `python main.py scorecard {report_type}`.", t0)
            else:
                age_text = f"{age_days:.1f}d old" if age_days is not None else "age unknown"
                _phase(f"⚠️ Actionable: scorecard is STALE ({age_text}) - refresh with `python main.py scorecard {report_type}`.", t0)

        actionable = out[out['Actionability'] == 'ACTIONABLE']
        if not actionable.empty:
            _phase(f"Actionable: {len(actionable)} on proven-win-rate tiers.", t0)
            return actionable
        # No scorecard yet (everything INSUFFICIENT_DATA) - keep all, ranked by score.
        _phase("Actionable: no proven tier yet (run `python main.py scorecard`); keeping by score.", t0)
        return out
    except Exception as e:
        logger.warning(f"Actionability step failed ({e}); skipping.")
        return df


def _apply_extension(df: pd.DataFrame, t0: float) -> pd.DataFrame:
    """Extension check is API-costed (per-symbol candle fetch) so it runs
    ONLY on the already-narrowed shortlist to respect the deadline."""
    try:
        from extension_score import annotate_extension
        out = annotate_extension(df)
        _phase(f"Extension: checked {len(out)} shortlisted names.", t0)
        return out
    except Exception as e:
        logger.warning(f"Extension step failed ({e}); skipping.")
        df['Extension_Flag'] = 'N/A'
        return df


def _apply_catalyst(df: pd.DataFrame, t0: float) -> pd.DataFrame:
    try:
        from nse_announcements import fetch_announcements, classify_materiality
        to_date = pd.Timestamp(datetime.now().date())
        ann = classify_materiality(fetch_announcements(to_date, to_date))
        if ann.empty:
            df['Catalyst'] = ''
            _phase("Catalyst: no announcements today.", t0)
            return df
        hi = set(ann[ann['Materiality'] == 'HIGH']['Symbol'])
        med = set(ann[ann['Materiality'] == 'MEDIUM']['Symbol'])
        df = df.copy()
        df['Catalyst'] = df['Symbol'].map(lambda s: 'HIGH' if s in hi else ('MED' if s in med else ''))
        _phase(f"Catalyst: {(df['Catalyst'] != '').sum()} shortlisted names have fresh filings.", t0)
        return df
    except Exception as e:
        logger.warning(f"Catalyst step failed ({e}); skipping.")
        df['Catalyst'] = ''
        return df


def _apply_reliability_flag(df: pd.DataFrame, t0: float) -> pd.DataFrame:
    """Informational-only, same convention as Catalyst/Institutional_Score:
    flags shortlist rows against real resolved-trade history (symbol_reliability.py),
    never filters. Loud-prints any UNRELIABLE name in the shortlist immediately -
    a repeated real losing pattern on a name the scan just ranked highly is
    exactly the kind of thing that should surface at decision time, not sit
    quietly in a CSV column."""
    try:
        from symbol_reliability import annotate_reliability
        out = annotate_reliability(df)
        bad = out[out['Reliability_Flag'] == 'UNRELIABLE']
        if not bad.empty:
            print(f"\n  ⚠️ RELIABILITY WARNING - {len(bad)} shortlisted name(s) have a repeated real losing pattern:")
            for _, row in bad.iterrows():
                print(f"      {row['Symbol']}: {row['Reliability_Note']}")
        _phase(f"Reliability: {len(bad)} flagged UNRELIABLE, {(out['Reliability_Flag']=='STRONG').sum()} flagged STRONG.", t0)
        return out
    except Exception as e:
        logger.warning(f"Reliability step failed ({e}); skipping.")
        df['Reliability_Flag'] = 'N/A'
        df['Reliability_Note'] = ''
        return df


def _print_btst_swing_bridge(t0: float) -> None:
    """Prints today's persisted BTST/SWING signals (if any) as a supplementary
    section - the "KEI problem" fix (flagged 2026-08-12): stockscan's main
    shortlist only ever reads momentum/EMFB reports, so a name that's strong
    on the BTST/SWING pipeline specifically (different scoring engine) was
    invisible here even when genuinely actionable, forcing manual
    cross-referencing between two systems. Requires orchestrator.run_eod_scan
    to have run today with persist=True (added 2026-08-21) - if EOD hasn't
    run yet today, or found nothing, this section is silently empty; it never
    blocks or slows the main shortlist above.
    """
    try:
        import sqlite3
        import os
        if not os.path.exists('signals.db'):
            return
        conn = sqlite3.connect('signals.db')
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            today = datetime.now(config.MARKET_TZ).strftime('%Y-%m-%d')
            df = pd.read_sql(
                "SELECT symbol, strategy, score, entry, stop, target, timestamp FROM signals "
                "WHERE date(timestamp) = ? AND strategy IN ('BTST', 'SWING') "
                "ORDER BY strategy, score DESC",
                conn, params=(today,),
            )
        finally:
            conn.close()

        if df.empty:
            return

        # 2026-08-21: BTST/SWING signals never carried a Confidence label at
        # write time (see symbol_history.py's SIGNAL_DB_STRATEGIES addition),
        # so this bridge used to show a bare score with no empirical framing.
        # That mattered more here than anywhere else this same-day: the first
        # `scorecard btst`/`scorecard swing` runs found BTST's score has
        # ~zero correlation with actual win rate (r=0.007) and SWING's is
        # INVERTED (its High tier: 46.8% vs Low tier: 53.4% - see
        # add_decision_scores in utils.py, an anti-chase design that isn't
        # panning out empirically). A bare high score here would read as
        # "good" when the data says the opposite. Deriving Confidence with
        # the identical formula used to build tier_performance.json, then
        # running every row through the same win-rate-gate momentum/EMFB
        # already get, closes that gap - unlike _apply_actionability above,
        # rows are NOT filtered down to ACTIONABLE-only here, since this
        # section's whole purpose is visibility (the "KEI problem" fix); a
        # SPECULATIVE label is exactly the information a human needs to see
        # attached to the row, not a reason to hide it.
        from symbol_history import _score_to_confidence
        from signal_quality import annotate_actionability
        df['Confidence'] = df['score'].apply(_score_to_confidence)
        annotated = [
            annotate_actionability(group, strategy.lower())
            for strategy, group in df.groupby('strategy')
        ]
        df = pd.concat(annotated, ignore_index=True) if annotated else df

        print("\n" + "-" * 100)
        print("BTST/SWING SIGNALS (separate pipeline - not part of the EMFB shortlist above)")
        print("-" * 100)
        cols = [c for c in ['symbol', 'strategy', 'score', 'Confidence', 'Actionability',
                             'Tier_Win_Rate_Pct', 'entry', 'stop', 'target', 'timestamp'] if c in df.columns]
        print(df[cols].to_string(index=False))
        print("-" * 100)
        _phase(f"BTST/SWING bridge: {len(df)} signal(s) from today's persisted EOD run.", t0)
    except Exception as e:
        logger.warning(f"BTST/SWING bridge section skipped due to an error: {e}", exc_info=True)


def run_stock_scan(report_type: str = 'momentum', top_n: int = 15) -> pd.DataFrame:
    """Full master scan. Returns the decision shortlist DataFrame."""
    import os
    t0 = time.time()
    now = datetime.now(config.MARKET_TZ)
    print("\n" + "=" * 100)
    print(f"STOCK SCAN - decision shortlist  |  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("=" * 100)

    missing = [f"{f} (via `{cmd}`)" for f, cmd in PRECOMPUTED_INPUTS.items() if not os.path.exists(f)]
    if missing:
        print("  ! Missing weekly-precomputed inputs - shortlist will be weaker until you run:")
        for m in missing:
            print(f"      {m}")

    df = _get_scan_report(report_type, t0)
    if df is None or df.empty:
        print("\nNo signals available. STOCK SCAN aborted.")
        return pd.DataFrame()

    score_col = 'EMFB_Score' if 'EMFB_Score' in df.columns else None
    df = _apply_fitness_filter(df, t0)
    df = _apply_actionability(df, report_type, t0)

    if score_col:
        df = df.sort_values(score_col, ascending=False)
    shortlist = df.head(top_n).copy()

    shortlist = _apply_extension(shortlist, t0)
    shortlist = _apply_catalyst(shortlist, t0)
    shortlist = _apply_reliability_flag(shortlist, t0)

    # Final ordering: proven-tier + fresh-entry (NORMAL extension) first, then by score.
    ext_rank = {'NORMAL': 0, 'EXTENDED': 1, 'VERY_EXTENDED': 2}
    shortlist['_ext'] = shortlist.get('Extension_Flag', pd.Series(['N/A'] * len(shortlist))).map(
        lambda f: ext_rank.get(f, 1)
    )
    sort_cols = ['_ext'] + ([score_col] if score_col else [])
    ascending = [True] + ([False] if score_col else [])
    shortlist = shortlist.sort_values(sort_cols, ascending=ascending).drop(columns=['_ext'])

    _print_shortlist(shortlist, score_col, t0)
    _print_btst_swing_bridge(t0)
    return shortlist


def _print_shortlist(df: pd.DataFrame, score_col: Optional[str], t0: float) -> None:
    print("\n" + "-" * 100)
    print("DECISION SHORTLIST (review these before 15:20 IST)")
    print("-" * 100)
    cols = [c for c in [
        'Symbol', score_col, 'Confidence', 'Tier_Win_Rate_Pct', 'Actionability',
        'Extension_Flag', 'Catalyst', 'Earnings_Risk', 'Trigger', 'Stop', 'Target'
    ] if c and c in df.columns]
    if df.empty:
        print("  (shortlist empty after filters)")
    else:
        print(df[cols].to_string(index=False))
    print("-" * 100)
    print(f"Done in {time.time() - t0:.1f}s.  Legend: Extension NORMAL=fresh entry, EXTENDED/VERY=chasing; "
          f"Catalyst HIGH/MED=fresh NSE filing; verify HIGH-RISK earnings before entry.")
    print("Reminder: shortlist ranks trade SETUPS - not buy advice. Read the chart/filing before acting.")

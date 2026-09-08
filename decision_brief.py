"""Consolidated decision brief - BTST / SWING / GRIND candidates in one command.

Built 2026-09-08. Answering "what's actually worth looking at today" previously
required manually stitching five tools together by hand: stockscan's shortlist,
Grind_Flag, Move_Stage, symbol_reliability, and a live price sanity check. This
module does that stitching once, into three clearly separated sections.

DESIGN RULES (learned from the first version's audit, same day):

1. SELF-SUFFICIENT, NOT REPORT-VINTAGE-DEPENDENT. The first version read
   Grind_Flag/Move_Stage as columns off whatever momentum report happened to be
   on disk. A report generated before those features existed silently lacked the
   columns, so the stage filter became a no-op and shipped EXTENDED names into a
   section whose entire purpose was excluding them (STLTECH at +33% 20d return
   sat in a list meant for early/midway entries). This version computes stage
   data itself via grind_scanner - cache-only, ~3s, zero API calls - so it
   behaves identically regardless of which report vintage it reads.

2. TRIGGER PRICES GET A SANITY CHECK. The momentum pipeline does not adjust for
   corporate actions (a known open gap - see project_weekly_audit_20260904
   memory). STLTECH's 2026-09-02 signal carried Trigger=405.15 against a real
   price near 825 - a stale pre-bonus level, roughly 50% wrong, sitting under a
   STRONG reliability flag. Any row whose Trigger diverges from the latest
   cached close by more than MAX_TRIGGER_DEVIATION_PCT is flagged PRICE_SUSPECT
   and dropped from the actionable sections rather than silently presented.
   Where the cache is too stale to judge, the row is marked UNVERIFIED and kept
   (absence of evidence is not evidence of a bad price).

3. STAGE FILTERING IS PER-STRATEGY, because the strategies have different
   tolerance for a move that has already run:
     BTST  - excludes EXTENDED and STALLING. A one-day hold on something that
             already ran is the worst risk/reward in the set.
     SWING - excludes STALLING only. A multi-day/week hold can ride a move that
             is already underway; it cannot ride one that has rolled over.
     GRIND - excludes EXTENDED and STALLING. The whole point is catching the
             move before it is obvious.

4. GRIND SORTS BY STAGE FIRST, NOT BY RETURN. Sorting by trailing return puts
   the most-advanced names on top, which is exactly backwards for a section
   meant to surface early entries. EARLY ranks above MIDWAY ranks above
   unclassified; score breaks ties within a stage.

5. TIMING IS NOT UNIFIED ACROSS STRATEGIES, and the brief says so out loud.
   BTST/SWING need a call before the 15:15 CAS window freezes F&O pricing (see
   project_stock_scan_playbook memory); GRIND is a multi-week entry with no
   same-day urgency. One "best time" for all three would be a lie.

Reads only data already on disk - NO new API calls.
"""
import logging
import os
import sqlite3
from datetime import datetime, time as dtime
from typing import Optional

import pandas as pd

import config

logger = logging.getLogger(__name__)

# A Trigger this far from the latest cached close is treated as a data artifact
# (corporate action, stale cache write) rather than a real level. STLTECH's
# known-bad case was ~50% off; normal intraday drift is a few percent.
MAX_TRIGGER_DEVIATION_PCT = 15.0

# Beyond this, the cached close is too old to judge a Trigger against.
MAX_CACHE_AGE_DAYS = 4

# Stage ordering for the GRIND section - earliest first.
_STAGE_RANK = {'EARLY': 0, 'MIDWAY': 1, '': 2}

# CAS window opens 15:15 IST; BTST/SWING calls must be made before it.
_CAS_START = dtime(15, 15)
# Last-hour / closing-strength factors are not meaningful before this.
_CLOSING_FACTORS_VALID_FROM = dtime(14, 30)


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

def _load_latest_momentum_report() -> Optional[pd.DataFrame]:
    from trigger_status import find_latest_report
    path = find_latest_report('momentum')
    if not path:
        return None
    try:
        df = pd.read_csv(path)
        df.attrs['source_path'] = path
        return df
    except Exception as e:
        logger.warning(f"Could not read momentum report {path}: {e}")
        return None


def _load_fitness() -> Optional[pd.DataFrame]:
    if not os.path.exists('universe_fitness.csv'):
        return None
    try:
        return pd.read_csv('universe_fitness.csv')
    except Exception as e:
        logger.warning(f"Could not read universe_fitness.csv: {e}")
        return None


def _load_reliability() -> Optional[pd.DataFrame]:
    try:
        from symbol_reliability import build_reliability_table
        table = build_reliability_table()
        return table if not table.empty else None
    except Exception as e:
        logger.warning(f"Could not build reliability table: {e}")
        return None


def _load_todays_signal_db_rows(strategy: str) -> pd.DataFrame:
    """Today's rows from the genuinely separate BTST/SWING scoring engine,
    persisted by `eod` (orchestrator.run_eod_scan with persist=True). Empty
    frame - not None - when unavailable, so callers can uniformly test .empty."""
    if not os.path.exists('signals.db'):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect('signals.db')
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            today = datetime.now(config.MARKET_TZ).strftime('%Y-%m-%d')
            return pd.read_sql(
                "SELECT symbol AS Symbol, score AS Engine_Score, entry AS Trigger, "
                "stop AS Stop, target AS Target FROM signals "
                "WHERE date(timestamp) = ? AND strategy = ? ORDER BY score DESC",
                conn, params=(today, strategy),
            )
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"signals.db read failed for {strategy}: {e}")
        return pd.DataFrame()


def _latest_cached_closes() -> pd.DataFrame:
    """Symbol -> (latest cached daily close, that bar's date). Reuses
    grind_scanner's own cache-path resolution so this agrees with the stage
    data rather than reading a different file for the same symbol."""
    try:
        from grind_scanner import _daily_cache_path
        import config as _cfg
        rows = []
        for symbol in _cfg.Universe.TARGET_UNIVERSE:
            path = _daily_cache_path(symbol)
            if not path:
                continue
            try:
                df = pd.read_parquet(path)
                if 'Close' not in df.columns or df.empty:
                    continue
                if 'Timestamp' in df.columns:
                    df = df.sort_values('Timestamp')
                    last_bar = pd.Timestamp(df['Timestamp'].iloc[-1]).tz_localize(None).normalize()
                else:
                    last_bar = pd.NaT
                rows.append({
                    'Symbol': symbol,
                    'Cached_Close': float(df['Close'].iloc[-1]),
                    'Cache_Last_Bar': last_bar,
                })
            except Exception:
                continue
        return pd.DataFrame(rows)
    except Exception as e:
        logger.warning(f"Could not read cached closes: {e}")
        return pd.DataFrame()


def _build_stage_table(momentum_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Grind_Flag + Move_Stage computed fresh, not read off the report. RS_1D
    is joined from the momentum report when available (it is more current than
    any file on disk); without it, HIDDEN/MIDWAY cannot be determined and those
    rows simply stay unclassified rather than being guessed at."""
    try:
        from grind_scanner import build_grind_table, annotate_grind, annotate_move_stage
        gt = build_grind_table()
        if gt.empty:
            return pd.DataFrame()
        if momentum_df is not None and 'RS_vs_Nifty' in momentum_df.columns:
            rs_lookup = momentum_df.set_index('Symbol')['RS_vs_Nifty']
            gt['RS_1D'] = gt['Symbol'].map(rs_lookup)
            gt['In_Momentum_Scan'] = gt['Symbol'].isin(momentum_df['Symbol'])
        else:
            gt['RS_1D'] = float('nan')
            gt['In_Momentum_Scan'] = False
        return annotate_move_stage(annotate_grind(gt))
    except Exception as e:
        logger.warning(f"Stage table build failed: {e}")
        return pd.DataFrame()


# --------------------------------------------------------------------------- #
# Enrichment
# --------------------------------------------------------------------------- #

def _enrich(df: pd.DataFrame, stage: pd.DataFrame, reliability: Optional[pd.DataFrame],
            closes: pd.DataFrame) -> pd.DataFrame:
    """Attaches stage, reliability and price-sanity columns. Left joins only -
    a symbol missing from any input keeps its row and gets a blank/neutral
    value, never gets dropped silently here (sections do their own explicit
    filtering afterwards, where it is visible)."""
    out = df.copy()
    if out.empty:
        for col in ('Grind_Flag', 'Move_Stage', 'Ret20d_Pct', 'Reliability_Flag',
                    'Win_Rate', 'Cached_Close', 'Trigger_Check'):
            out[col] = pd.Series(dtype='object')
        return out

    if not stage.empty:
        keep = [c for c in ['Symbol', 'Grind_Flag', 'Move_Stage', 'Ret20d_Pct', 'Ret10d_Pct'] if c in stage.columns]
        out = out.merge(stage[keep], on='Symbol', how='left')
    for col in ('Grind_Flag', 'Move_Stage'):
        if col not in out.columns:
            out[col] = ''
        out[col] = out[col].fillna('')

    if reliability is not None:
        rel_cols = [c for c in ['Symbol', 'Reliability_Flag', 'Win_Rate', 'Avg_Return_Pct'] if c in reliability.columns]
        out = out.merge(reliability[rel_cols], on='Symbol', how='left')
    if 'Reliability_Flag' not in out.columns:
        out['Reliability_Flag'] = 'INSUFFICIENT_DATA'
    out['Reliability_Flag'] = out['Reliability_Flag'].fillna('INSUFFICIENT_DATA')

    out = _check_triggers(out, closes)
    return out


def _check_triggers(df: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame:
    """Adds Trigger_Check: OK / PRICE_SUSPECT / UNVERIFIED.

    PRICE_SUSPECT means the Trigger disagrees with the latest cached close by
    more than MAX_TRIGGER_DEVIATION_PCT while that cache is recent enough to be
    believed - the STLTECH corporate-action case. UNVERIFIED means the cache is
    too old (or absent) to judge, which is not the same thing as a bad price
    and must not be treated as one."""
    out = df.copy()
    if closes.empty or 'Trigger' not in out.columns:
        out['Cached_Close'] = float('nan')
        out['Trigger_Check'] = 'UNVERIFIED'
        return out

    out = out.merge(closes, on='Symbol', how='left')
    today = pd.Timestamp.now().normalize()
    age_days = (today - out['Cache_Last_Bar']).dt.days
    fresh = out['Cache_Last_Bar'].notna() & (age_days <= MAX_CACHE_AGE_DAYS)

    import numpy as np
    # A zero/absent cached close would make this infinite rather than large -
    # treat that as "cannot judge" (NaN -> UNVERIFIED), not as a huge deviation.
    deviation = ((out['Trigger'] - out['Cached_Close']).abs() / out['Cached_Close']) * 100
    deviation = deviation.replace([np.inf, -np.inf], np.nan)

    out['Trigger_Dev_Pct'] = deviation.round(1)
    suspect = fresh & deviation.notna() & (deviation > MAX_TRIGGER_DEVIATION_PCT)
    out['Trigger_Check'] = 'UNVERIFIED'
    out.loc[fresh & deviation.notna(), 'Trigger_Check'] = 'OK'
    out.loc[suspect, 'Trigger_Check'] = 'PRICE_SUSPECT'
    return out


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

def _fitness_eligible(fitness: Optional[pd.DataFrame], tag: str) -> set:
    if fitness is None or 'All_Tags' not in fitness.columns:
        return set()
    mask = fitness['All_Tags'].fillna('').str.contains(tag, regex=False)
    return set(fitness.loc[mask, 'Symbol'])


def _apply_section_filters(df: pd.DataFrame, exclude_stages: list) -> tuple:
    """Returns (kept, dropped_reasons dict, needs_verification df).

    Stale-cache rows are SEPARATED, not kept and not silently dropped. Without
    this, a stale row slips through every stage filter for the wrong reason:
    annotate_move_stage() deliberately refuses to classify on stale data, so
    Move_Stage comes back blank, so a filter excluding EXTENDED never matches
    it. Measured 2026-09-08: STLTECH carried Ret20d_Pct 29.4 (comfortably
    EXTENDED) and a known-bad Trigger of 405.15 against a real price near 825,
    and sat in the GRIND section - the one section whose entire purpose is
    excluding already-extended names - under a STRONG reliability flag, purely
    because its cache was too old to classify. Unverifiable is its own state
    and gets its own section."""
    if df.empty:
        return df, {}, df

    dropped = {}
    out = df

    today = pd.Timestamp.now().normalize()
    if 'Cache_Last_Bar' in out.columns:
        age = (today - out['Cache_Last_Bar']).dt.days
        stale_mask = out['Cache_Last_Bar'].isna() | (age > MAX_CACHE_AGE_DAYS)
    else:
        stale_mask = pd.Series(False, index=out.index)

    needs_verification = out[stale_mask].copy()
    out = out[~stale_mask]
    if len(needs_verification):
        dropped['stale cache -> needs verification'] = len(needs_verification)

    n = len(out)
    out = out[out['Reliability_Flag'] != 'UNRELIABLE']
    if n - len(out):
        dropped['UNRELIABLE'] = n - len(out)

    n = len(out)
    out = out[~out['Move_Stage'].isin(exclude_stages)]
    if n - len(out):
        dropped['/'.join(exclude_stages)] = n - len(out)

    n = len(out)
    out = out[out['Trigger_Check'] != 'PRICE_SUSPECT']
    if n - len(out):
        dropped['PRICE_SUSPECT'] = n - len(out)

    return out, dropped, needs_verification


def _sort_by_score(df: pd.DataFrame) -> pd.DataFrame:
    for col in ('EMFB_Score', 'Engine_Score'):
        if col in df.columns:
            return df.sort_values(col, ascending=False)
    return df


def _sort_grind(df: pd.DataFrame) -> pd.DataFrame:
    """Stage first (EARLY before MIDWAY before unclassified), score as the
    tie-break. Sorting by trailing return instead - as the first version did -
    surfaces the most-advanced names first, which is backwards for a section
    whose purpose is catching a move before it is obvious."""
    if df.empty:
        return df
    out = df.copy()
    out['_stage_rank'] = out['Move_Stage'].map(lambda s: _STAGE_RANK.get(s, 3))
    score_col = 'EMFB_Score' if 'EMFB_Score' in out.columns else None
    sort_cols = ['_stage_rank'] + ([score_col] if score_col else [])
    ascending = [True] + ([False] if score_col else [])
    return out.sort_values(sort_cols, ascending=ascending).drop(columns=['_stage_rank'])


def build_decision_brief() -> dict:
    """Returns {'btst', 'swing', 'grind': DataFrames, 'meta': dict}. Never
    raises - a failed input degrades that section to empty with the reason in
    meta, matching stock_scan.py's fail-soft phase convention."""
    meta: dict = {}
    mom = _load_latest_momentum_report()
    fitness = _load_fitness()
    reliability = _load_reliability()
    stage = _build_stage_table(mom)
    closes = _latest_cached_closes()

    meta['momentum_report'] = mom.attrs.get('source_path') if mom is not None else None
    meta['momentum_report_is_today'] = bool(
        meta['momentum_report']
        and datetime.now(config.MARKET_TZ).strftime('%Y%m%d') in meta['momentum_report']
    )
    meta['fitness_available'] = fitness is not None
    meta['reliability_available'] = reliability is not None
    meta['stage_rows'] = len(stage)
    meta['dropped'] = {}

    def _strategy_frame(strategy: str, tag: str) -> pd.DataFrame:
        db_rows = _load_todays_signal_db_rows(strategy)
        if not db_rows.empty:
            meta[f'{strategy.lower()}_source'] = f'signals.db ({strategy} engine)'
            # Carry the momentum report's context onto the engine's rows so this
            # path is not less informative than the fallback (the first version's
            # flaw - the "preferred" path dropped stage/confidence entirely).
            if mom is not None:
                ctx = [c for c in ['Symbol', 'EMFB_Score', 'Confidence'] if c in mom.columns]
                if len(ctx) > 1:
                    db_rows = db_rows.merge(mom[ctx], on='Symbol', how='left')
            return db_rows
        meta[f'{strategy.lower()}_source'] = 'fitness-tag fallback (eod has not persisted today)'
        if mom is None:
            return pd.DataFrame()
        eligible = _fitness_eligible(fitness, tag)
        return mom[mom['Symbol'].isin(eligible)].copy() if eligible else mom.iloc[0:0].copy()

    btst = _enrich(_strategy_frame('BTST', 'BTST_ELIGIBLE'), stage, reliability, closes)
    swing = _enrich(_strategy_frame('SWING', 'SWING_ELIGIBLE'), stage, reliability, closes)

    if mom is not None and not stage.empty:
        grind_syms = set(stage.loc[stage['Grind_Flag'].isin(['GRIND', 'HIDDEN', 'QUALITY']), 'Symbol'])
        grind = _enrich(mom[mom['Symbol'].isin(grind_syms)].copy(), stage, reliability, closes)
    else:
        grind = _enrich(pd.DataFrame(), stage, reliability, closes)

    btst, meta['dropped']['btst'], stale_btst = _apply_section_filters(btst, ['EXTENDED', 'STALLING'])
    swing, meta['dropped']['swing'], stale_swing = _apply_section_filters(swing, ['STALLING'])
    grind, meta['dropped']['grind'], stale_grind = _apply_section_filters(grind, ['EXTENDED', 'STALLING'])

    needs_verification = pd.concat([stale_btst, stale_swing, stale_grind], ignore_index=True)
    if not needs_verification.empty:
        needs_verification = needs_verification.drop_duplicates(subset=['Symbol'])
        needs_verification = _sort_by_score(needs_verification)

    return {
        'btst': _sort_by_score(btst),
        'swing': _sort_by_score(swing),
        'grind': _sort_grind(grind),
        'needs_verification': needs_verification,
        'meta': meta,
    }


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

def _timing_notes() -> list:
    now = datetime.now(config.MARKET_TZ).time()
    notes = []
    if now >= _CAS_START:
        notes.append("⚠️  Past 15:15 IST - the CAS window has opened, F&O pricing is frozen/"
                     "auction-set. BTST/SWING below are NOT actionable same-day; treat as "
                     "next-session prep.")
    elif now < _CLOSING_FACTORS_VALID_FROM:
        notes.append("⚠️  Before 14:30 IST - last-hour volume and closing-strength factors are "
                     "not meaningful yet, so BTST conviction is weaker than it will look later. "
                     "Re-run ~14:55-15:10 for the real call.")
    else:
        notes.append("✅ Inside the 14:30-15:15 window - this is the right time for a same-day "
                     "BTST/SWING call.")
    notes.append("GRIND is a multi-week entry with no same-day deadline - readable at any time.")
    return notes


def print_decision_brief(brief: dict, top_n: int = 10) -> None:
    meta = brief['meta']
    print("\n" + "=" * 108)
    print(f"DECISION BRIEF - {datetime.now(config.MARKET_TZ).strftime('%Y-%m-%d %H:%M:%S')} IST")
    print("=" * 108)

    for note in _timing_notes():
        print(note)

    if not meta.get('momentum_report_is_today'):
        print("⚠️  Momentum report is NOT from today - run `python main.py momentum` first; "
              "everything below is stale.")
    print(f"\nBTST source : {meta.get('btst_source', 'unavailable')}")
    print(f"SWING source: {meta.get('swing_source', 'unavailable')}")
    print(f"Stage data  : {meta.get('stage_rows', 0)} symbols (computed fresh from daily cache)")

    def _section(name: str, df: pd.DataFrame, cols: list, dropped: dict) -> None:
        print("\n" + "-" * 108)
        drop_text = ", ".join(f"{v} {k}" for k, v in dropped.items()) if dropped else "none"
        print(f"{name} ({len(df)})   [excluded: {drop_text}]")
        print("-" * 108)
        if df.empty:
            print("  (none)")
            return
        present = [c for c in cols if c in df.columns]
        print(df[present].head(top_n).to_string(index=False))

    common = ['Symbol', 'EMFB_Score', 'Engine_Score', 'Confidence', 'Move_Stage', 'Grind_Flag',
              'Reliability_Flag', 'Win_Rate', 'Trigger', 'Stop', 'Target', 'Trigger_Check']
    _section("BTST CANDIDATES", brief['btst'], common, meta['dropped'].get('btst', {}))
    _section("SWING CANDIDATES", brief['swing'], common, meta['dropped'].get('swing', {}))
    grind_cols = ['Symbol', 'EMFB_Score', 'Move_Stage', 'Grind_Flag', 'Ret20d_Pct',
                  'Reliability_Flag', 'Win_Rate', 'Trigger', 'Stop', 'Target', 'Trigger_Check']
    _section("GRIND CANDIDATES", brief['grind'], grind_cols, meta['dropped'].get('grind', {}))

    nv = brief.get('needs_verification', pd.DataFrame())
    if nv is not None and not nv.empty:
        print("\n" + "-" * 108)
        print(f"⚠️  NEEDS VERIFICATION ({len(nv)}) - daily cache older than "
              f"{MAX_CACHE_AGE_DAYS} days, so neither the move stage nor the trigger price "
              f"could be checked")
        print("-" * 108)
        nv_cols = ['Symbol', 'EMFB_Score', 'Confidence', 'Ret20d_Pct', 'Reliability_Flag',
                   'Win_Rate', 'Trigger', 'Cached_Close', 'Cache_Last_Bar']
        present = [c for c in nv_cols if c in nv.columns]
        print(nv[present].head(top_n).to_string(index=False))
        print("These are NOT rejected - they are unconfirmable. Refresh with "
              "`python main.py analyze <SYMBOL> --force-refresh` before acting on any of them.")

    print("\n" + "=" * 108)
    print("Candidates, not buy advice. Already excluded: UNRELIABLE names (all sections), "
          "STALLING (all), EXTENDED (BTST/GRIND - SWING keeps them by design), and "
          "PRICE_SUSPECT rows whose trigger disagrees with the cached close by "
          f">{MAX_TRIGGER_DEVIATION_PCT:.0f}%.")
    print("UNVERIFIED trigger = cache too stale to check, not a bad price - verify manually.")
    print("=" * 108)


def run_decision_brief(top_n: int = 10) -> dict:
    brief = build_decision_brief()
    print_decision_brief(brief, top_n=top_n)
    return brief

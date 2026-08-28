"""Cross-report memory for EMFB/momentum/BTST/SWING scans.

EMFB/momentum: built from the existing report CSV files on disk, not
reports.db. Every emfb_report_*.csv / momentum_report_*.csv is a standalone
snapshot; nothing in the pipeline connects one scan's output to the next.
That's why APARINDS first appearing on 2026-07-31 (rank #5, score 80.3)
looked like a brand-new idea on 2026-08-04 - answering "when did this first
show up, and how has its score moved since" required manually grepping four
separate CSV files (see the 2026-08-07 session that led to this module).

reports.db's emfb_signals/momentum_signals tables would be the natural home
for this, but that table doesn't currently exist despite the pipeline
calling db.log_dataframe() after every run - investigated 2026-08-07,
inconclusive (see the print() added to emfb.py's/momentum_scanner.py's
_generate_report failure handlers for next time it happens). This module
sidesteps that entirely and reads the CSV files directly, which reliably
exist for every past run.

BTST/SWING: 2026-08-21 addition. These never produce CSV reports - they only
ever get logged to signals.db (see database.py's SignalDB, orchestrator.py's
persist=True on EOD's BTST/SWING calls). There was no performance-tracking
path for either strategy at all until now, discovered when `scorecard btst`
was asked for and 'btst' wasn't a recognized report_type. build_signal_db_history
reads signals.db instead of globbing CSVs; everything downstream
(performance_tracker.py, signal_quality.py) is report_type-generic and needed
no changes once build_history could return a same-shaped frame for these too.
"""
import glob
import logging
import os
import sqlite3
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

REPORT_GLOBS = {
    'emfb': 'emfb_report_*.csv',
    'momentum': 'momentum_report_*.csv',
}

# BTST/SWING signals.db rows never had a Confidence label written at insert
# time (only EMFB's rank_and_score_emfb assigns one) - so it's derived here
# at scorecard-build time instead, using the identical >75/>60 cutoffs
# scanner_engine.py uses for EMFB's own High/Medium/Low, since BTST_Final_Score
# and Decision_Score are both on the same 0-100 scale as EMFB_Score.
SIGNAL_DB_STRATEGIES = {'btst': 'BTST', 'swing': 'SWING'}


def _score_to_confidence(score: float) -> str:
    if score > 75:
        return 'High'
    if score > 60:
        return 'Medium'
    return 'Low'


def build_signal_db_history(strategy: str) -> pd.DataFrame:
    """signals.db equivalent of build_history() for BTST/SWING - one row per
    persisted signal, shaped to match HISTORY_COLUMNS so build_scorecard()
    (which only depends on that shape) works unchanged."""
    if not os.path.exists('signals.db'):
        return pd.DataFrame(columns=HISTORY_COLUMNS + ['Report_File'])
    conn = sqlite3.connect('signals.db')
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        df = pd.read_sql(
            "SELECT symbol AS Symbol, score AS EMFB_Score, entry AS Trigger, "
            "stop AS Stop, target AS Target, timestamp FROM signals WHERE strategy = ? "
            "AND entry IS NOT NULL AND stop IS NOT NULL AND target IS NOT NULL "
            "AND timestamp IS NOT NULL",
            conn, params=(strategy,),
        )
    finally:
        conn.close()
    if df.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS + ['Report_File'])
    df['Sector'] = pd.NA
    df['Rank'] = pd.NA
    df['Earnings_Risk'] = pd.NA
    df['Confidence'] = df['EMFB_Score'].apply(_score_to_confidence)
    df['Report_File'] = 'signals.db'
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    # A handful of rows (older schema, or a row written mid-migration) can
    # still fail to parse even after the NOT NULL filter above - build_scorecard
    # crashes on NaT (performance_tracker.py's report_date.normalize() call),
    # so drop them here rather than pushing that fragility downstream.
    df = df.dropna(subset=['timestamp'])
    return df[HISTORY_COLUMNS + ['Report_File']].sort_values('timestamp').reset_index(drop=True)

# Columns pulled from each report file. Older report formats (e.g. the
# 2026-07-08 EMFB run, before Sector/Earnings_Risk were added to the
# scanner's output) lack some of these - build_history tolerates missing
# columns rather than requiring every report to share one exact schema.
HISTORY_COLUMNS = [
    'Symbol', 'Sector', 'EMFB_Score', 'Rank', 'Confidence',
    'Trigger', 'Stop', 'Target', 'Earnings_Risk', 'timestamp',
]


def _list_reports(report_type: str) -> list[str]:
    pattern = REPORT_GLOBS.get(report_type)
    if pattern is None:
        raise ValueError(
            f"Unknown report_type '{report_type}'; expected one of "
            f"{list(REPORT_GLOBS) + list(SIGNAL_DB_STRATEGIES)}"
        )
    return sorted(glob.glob(pattern))


def build_history(report_type: str) -> pd.DataFrame:
    """Concatenates every past report of `report_type` into one long table,
    one row per (report, symbol) pair, sorted chronologically. Empty
    DataFrame if no reports exist yet.

    'btst'/'swing' route to signals.db (build_signal_db_history) instead of
    the CSV-glob path below - those strategies never produce report files.
    """
    if report_type in SIGNAL_DB_STRATEGIES:
        return build_signal_db_history(SIGNAL_DB_STRATEGIES[report_type])

    paths = _list_reports(report_type)
    if not paths:
        return pd.DataFrame(columns=HISTORY_COLUMNS + ['Report_File'])

    frames = []
    for path in paths:
        try:
            df = pd.read_csv(path)
        except Exception as e:
            logger.warning(f"Skipping unreadable report {path}: {e}")
            continue
        for col in HISTORY_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[HISTORY_COLUMNS].copy()
        df['Report_File'] = path
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=HISTORY_COLUMNS + ['Report_File'])

    history = pd.concat(frames, ignore_index=True)
    history['timestamp'] = pd.to_datetime(history['timestamp'], errors='coerce')
    return history.sort_values('timestamp').reset_index(drop=True)


def symbol_history(symbol: str, report_type: str) -> pd.DataFrame:
    """One symbol's appearances across every past report, chronological -
    answers 'when did this first show up and how has its score/rank moved'
    without grepping report files by hand."""
    history = build_history(report_type)
    if history.empty:
        return history
    return history[history['Symbol'].str.upper() == symbol.upper()].reset_index(drop=True)


def watchlist_diff(report_type: str) -> dict:
    """Compares the two most recent reports of `report_type`. Returns a dict
    with 'new' (in latest, not previous), 'dropped' (was there, isn't now),
    and 'continuing' (in both, with score delta) DataFrames, plus the two
    report paths compared. None values / empty frames if fewer than 2
    reports exist yet.
    """
    paths = _list_reports(report_type)
    if len(paths) < 2:
        return {'previous_report': paths[0] if paths else None, 'latest_report': paths[-1] if paths else None,
                'new': pd.DataFrame(), 'dropped': pd.DataFrame(), 'continuing': pd.DataFrame()}

    previous_path, latest_path = paths[-2], paths[-1]
    previous_df = pd.read_csv(previous_path)[['Symbol', 'EMFB_Score', 'Rank']]
    latest_df = pd.read_csv(latest_path)[['Symbol', 'EMFB_Score', 'Rank']]

    prev_symbols = set(previous_df['Symbol'])
    latest_symbols = set(latest_df['Symbol'])

    new_df = latest_df[latest_df['Symbol'].isin(latest_symbols - prev_symbols)].sort_values('Rank').reset_index(drop=True)
    dropped_df = previous_df[previous_df['Symbol'].isin(prev_symbols - latest_symbols)].sort_values('Rank').reset_index(drop=True)

    continuing_symbols = latest_symbols & prev_symbols
    continuing_df = pd.DataFrame()
    if continuing_symbols:
        merged = latest_df[latest_df['Symbol'].isin(continuing_symbols)].merge(
            previous_df, on='Symbol', suffixes=('_Latest', '_Previous')
        )
        merged['Score_Delta'] = merged['EMFB_Score_Latest'] - merged['EMFB_Score_Previous']
        merged['Rank_Delta'] = merged['Rank_Previous'] - merged['Rank_Latest']  # positive = moved up (better rank)
        continuing_df = merged.sort_values('Score_Delta', ascending=False).reset_index(drop=True)

    return {
        'previous_report': previous_path,
        'latest_report': latest_path,
        'new': new_df,
        'dropped': dropped_df,
        'continuing': continuing_df,
    }


def print_symbol_history(symbol: str, history: pd.DataFrame) -> None:
    if history.empty:
        print(f"\nNo history found for {symbol}.")
        return
    print("\n" + "=" * 90)
    print(f"HISTORY: {symbol.upper()}")
    print("=" * 90)
    cols = ['timestamp', 'EMFB_Score', 'Rank', 'Confidence', 'Trigger', 'Stop', 'Target', 'Earnings_Risk']
    print(history[cols].to_string(index=False))
    first_seen = history['timestamp'].iloc[0]
    print(f"\nFirst seen: {first_seen} | Appearances: {len(history)}")
    print("=" * 90)


def print_watchlist_diff(diff: dict) -> None:
    print("\n" + "=" * 90)
    print(f"WATCHLIST DIFF: {diff.get('previous_report')} -> {diff.get('latest_report')}")
    print("=" * 90)

    new_df, dropped_df, continuing_df = diff['new'], diff['dropped'], diff['continuing']

    print(f"\nNEW ({len(new_df)}):")
    print(new_df.to_string(index=False) if not new_df.empty else "  (none)")

    print(f"\nDROPPED ({len(dropped_df)}):")
    print(dropped_df.to_string(index=False) if not dropped_df.empty else "  (none)")

    print(f"\nCONTINUING ({len(continuing_df)}):")
    if not continuing_df.empty:
        cols = ['Symbol', 'EMFB_Score_Previous', 'EMFB_Score_Latest', 'Score_Delta', 'Rank_Previous', 'Rank_Latest', 'Rank_Delta']
        print(continuing_df[cols].to_string(index=False))
    else:
        print("  (none)")
    print("=" * 90)

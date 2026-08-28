"""Per-symbol reliability flag - "this name has a real, repeated track record
of being wrong (or right)," derived from resolved trade outcomes, not scan
confidence.

Built 2026-08-25 after a manual analysis of `signals.db`'s `outcomes` table
(7,249 resolved BTST/SWING trades, backfilled by `historical_generator.py`
over roughly 2026-05-14 to 2026-07-06) found 15 symbols with real, repeated
losing patterns (e.g. ENGINERSIN: 29 resolved trades, 0% win rate, -6.4% avg
return) sitting right next to symbols with a genuine edge (PARAS: 80 trades,
86% win rate, +8.1% avg return) - the scan currently treats both the same
way. This module makes that distinction visible without hiding it: it is
INFORMATIONAL ONLY, same convention as Institutional_Score and Catalyst in
stock_scan.py - it never filters or reorders the shortlist, only flags rows
so a human can weight them. Reasons to keep it informational rather than
filtering, matching [[project-universe-composition]]'s Institutional_Score
caution:
  - the backing data is a single ~7-week window, not a proven-stable pattern
    across regimes yet.
  - it currently only covers BTST/SWING outcomes (the only strategies with
    resolved forward-return history) - momentum/EMFB symbols get looked up
    against the same table, which may or may not generalize to their setup.
Re-evaluate promoting UNRELIABLE to an actual filter only after this has
been checked against a second, later backtest window and the flagged names
are still showing the same pattern.
"""
import logging
import sqlite3
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

SIGNALS_DB_PATH = "signals.db"
RELIABILITY_OUTPUT_PATH = "symbol_reliability.csv"

MIN_TRADES = 10
UNRELIABLE_MAX_WIN_RATE = 0.30
UNRELIABLE_MAX_AVG_RETURN_PCT = -2.0
STRONG_MIN_WIN_RATE = 0.65
STRONG_MIN_AVG_RETURN_PCT = 3.0


def build_reliability_table(min_trades: int = MIN_TRADES) -> pd.DataFrame:
    """Per-symbol win rate / avg return from every resolved (non-skipped)
    outcome on record, classified into STRONG / UNRELIABLE / NEUTRAL /
    INSUFFICIENT_DATA. Returns one row per symbol that has ever had a
    resolved trade - most rows will be NEUTRAL or INSUFFICIENT_DATA, which
    is expected and not itself meaningful (absence of a repeated pattern,
    not evidence the name is fine)."""
    import os
    if not os.path.exists(SIGNALS_DB_PATH):
        logger.warning(f"{SIGNALS_DB_PATH} not found; no reliability data available.")
        return pd.DataFrame()

    conn = sqlite3.connect(SIGNALS_DB_PATH)
    try:
        outcomes = pd.read_sql(
            "SELECT symbol, return_pct FROM outcomes WHERE outcome != 'SKIPPED_NO_FUTURE_CANDLES'",
            conn,
        )
    finally:
        conn.close()

    if outcomes.empty:
        return pd.DataFrame()

    outcomes['is_win'] = outcomes['return_pct'] > 0
    agg = outcomes.groupby('symbol').agg(
        Trades=('return_pct', 'count'),
        Win_Rate=('is_win', 'mean'),
        Avg_Return_Pct=('return_pct', 'mean'),
    ).reset_index().rename(columns={'symbol': 'Symbol'})

    def _classify(row) -> str:
        if row['Trades'] < min_trades:
            return 'INSUFFICIENT_DATA'
        if row['Win_Rate'] <= UNRELIABLE_MAX_WIN_RATE and row['Avg_Return_Pct'] <= UNRELIABLE_MAX_AVG_RETURN_PCT:
            return 'UNRELIABLE'
        if row['Win_Rate'] >= STRONG_MIN_WIN_RATE and row['Avg_Return_Pct'] >= STRONG_MIN_AVG_RETURN_PCT:
            return 'STRONG'
        return 'NEUTRAL'

    agg['Reliability_Flag'] = agg.apply(_classify, axis=1)
    agg['Win_Rate'] = (agg['Win_Rate'] * 100).round(1)
    agg['Avg_Return_Pct'] = agg['Avg_Return_Pct'].round(2)

    try:
        agg.sort_values('Avg_Return_Pct', ascending=False).to_csv(RELIABILITY_OUTPUT_PATH, index=False)
    except OSError as e:
        logger.warning(f"Failed to save {RELIABILITY_OUTPUT_PATH}: {e}")

    return agg


def annotate_reliability(df: pd.DataFrame, symbol_col: str = 'Symbol') -> pd.DataFrame:
    """Adds 'Reliability_Flag' and 'Reliability_Note' columns to any report
    DataFrame by looking up each row's symbol. Rows for symbols with no
    resolved-trade history (the common case for names outside BTST/SWING's
    coverage) get 'INSUFFICIENT_DATA', not a blank - so a caller can't
    mistake "never checked" for "checked and fine". Never drops or reorders
    rows."""
    out = df.copy()
    table = build_reliability_table()
    if table.empty or symbol_col not in out.columns:
        out['Reliability_Flag'] = 'INSUFFICIENT_DATA'
        out['Reliability_Note'] = ''
        return out

    lookup = table.set_index('Symbol')[['Reliability_Flag', 'Trades', 'Win_Rate', 'Avg_Return_Pct']]

    def _note(sym: str) -> str:
        if sym not in lookup.index:
            return ''
        row = lookup.loc[sym]
        flag = row['Reliability_Flag']
        if flag not in ('UNRELIABLE', 'STRONG'):
            return ''
        return f"{int(row['Trades'])} trades, {row['Win_Rate']:.0f}% win, {row['Avg_Return_Pct']:+.1f}% avg return"

    out['Reliability_Flag'] = out[symbol_col].map(
        lambda s: lookup.loc[s, 'Reliability_Flag'] if s in lookup.index else 'INSUFFICIENT_DATA'
    )
    out['Reliability_Note'] = out[symbol_col].map(_note)
    return out


def print_reliability_report(min_trades: int = MIN_TRADES) -> None:
    table = build_reliability_table(min_trades)
    print("\n" + "=" * 100)
    print("SYMBOL RELIABILITY - resolved BTST/SWING trade history, not scan confidence")
    print("=" * 100)
    if table.empty:
        print("No resolved outcome history available.")
        print("=" * 100)
        return

    strong = table[table['Reliability_Flag'] == 'STRONG'].sort_values('Avg_Return_Pct', ascending=False)
    unreliable = table[table['Reliability_Flag'] == 'UNRELIABLE'].sort_values('Avg_Return_Pct')
    cols = ['Symbol', 'Trades', 'Win_Rate', 'Avg_Return_Pct']

    if not strong.empty:
        print(f"\n--- STRONG ({len(strong)}) - real, repeated edge ---")
        print(strong[cols].to_string(index=False))
    if not unreliable.empty:
        print(f"\n--- UNRELIABLE ({len(unreliable)}) - real, repeated losing pattern ---")
        print(unreliable[cols].to_string(index=False))

    n_sufficient = (table['Reliability_Flag'] != 'INSUFFICIENT_DATA').sum()
    print(f"\n{n_sufficient}/{len(table)} symbols have >= {min_trades} resolved trades; the rest are unclassified, not cleared.")
    print(f"Full table saved to {RELIABILITY_OUTPUT_PATH}.")
    print("=" * 100)

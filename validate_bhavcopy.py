"""Reliability check for nse_daily_history.py's bhavcopy-sourced daily
candles against DataBroker's own (Angel-sourced) daily candles.

Deliberately built as a repeatable, standalone check rather than folded
into a one-off test run: the plan (per the 2026-08-10 rate-limit
investigation) is to validate bhavcopy over several sessions before
bringing the same daily-fetch swap to discovery.py (V1.0) - that's a much
bigger-blast-radius change (BTST/SWING's actual live scoring, with a
validated track record behind it) than momentum_scanner.py, which already
adopted it. This script is what "a few more days of validation" means in
practice: re-run it each session; only proceed to the discovery.py swap
once it's shown clean across multiple independent days with no material
discrepancies.

A "material" discrepancy is intentionally a real threshold, not exact
equality - Angel's live feed and NSE's own bhavcopy can legitimately differ
by small amounts (rounding, corporate-action timing, feed-provider
adjustment differences), same as the SAIL close mismatches already noticed
in passing this session (176.74 vs 177.37 on the same date, ~0.35% apart -
that's normal feed noise, not a bug in either source).
"""
import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from data_broker import DataBroker
from nse_daily_history import NSEDailyHistory

logger = logging.getLogger(__name__)

MATERIAL_DISCREPANCY_PCT = 1.0  # close-price difference beyond this is flagged
DEFAULT_SAMPLE_DAYS = 10


def validate_symbols(symbols: list[str], sample_days: int = DEFAULT_SAMPLE_DAYS,
                      broker: Optional[DataBroker] = None) -> pd.DataFrame:
    """For each symbol, compares the last `sample_days` closes from
    bhavcopy vs the broker. Returns one row per (symbol, date) with both
    closes, the % difference, and a Flag column (OK / MISSING_BHAVCOPY /
    MISSING_BROKER / MATERIAL_DIFF)."""
    owns_broker = broker is None
    broker = broker if broker is not None else DataBroker()
    history_feed = NSEDailyHistory()

    rows = []
    try:
        bhav = history_feed.fetch_daily_history_bulk(symbols, days_back=sample_days)
        for sym in symbols:
            bhav_df = bhav.get(sym, pd.DataFrame())
            broker_df = broker.fetch_daily_candles(sym, days_back=sample_days + 5)
            if broker_df.empty:
                rows.append({'Symbol': sym, 'Date': None, 'Bhavcopy_Close': None,
                             'Broker_Close': None, 'Diff_Pct': None, 'Flag': 'MISSING_BROKER'})
                continue

            broker_by_date = {ts.date(): float(row['close']) for ts, row in broker_df.iterrows()}
            bhav_by_date = ({} if bhav_df.empty else
                             {pd.Timestamp(ts).date(): float(c) for ts, c in
                              zip(bhav_df['Timestamp'], bhav_df['Close'])})

            all_dates = sorted(set(broker_by_date) | set(bhav_by_date), reverse=True)[:sample_days]
            for d in all_dates:
                b_close = bhav_by_date.get(d)
                k_close = broker_by_date.get(d)
                if b_close is None:
                    rows.append({'Symbol': sym, 'Date': d, 'Bhavcopy_Close': None,
                                 'Broker_Close': k_close, 'Diff_Pct': None, 'Flag': 'MISSING_BHAVCOPY'})
                    continue
                if k_close is None:
                    rows.append({'Symbol': sym, 'Date': d, 'Bhavcopy_Close': b_close,
                                 'Broker_Close': None, 'Diff_Pct': None, 'Flag': 'MISSING_BROKER'})
                    continue
                diff_pct = abs(b_close - k_close) / k_close * 100 if k_close else 0.0
                flag = 'MATERIAL_DIFF' if diff_pct > MATERIAL_DISCREPANCY_PCT else 'OK'
                rows.append({'Symbol': sym, 'Date': d, 'Bhavcopy_Close': b_close,
                             'Broker_Close': k_close, 'Diff_Pct': round(diff_pct, 3), 'Flag': flag})
    finally:
        history_feed.close()
        if owns_broker:
            broker.close_session()

    return pd.DataFrame(rows)


def summarize(results: pd.DataFrame) -> dict:
    if results.empty:
        return {'total_checks': 0}
    return {
        'total_checks': len(results),
        'ok': int((results['Flag'] == 'OK').sum()),
        'material_diff': int((results['Flag'] == 'MATERIAL_DIFF').sum()),
        'missing_bhavcopy': int((results['Flag'] == 'MISSING_BHAVCOPY').sum()),
        'missing_broker': int((results['Flag'] == 'MISSING_BROKER').sum()),
        'max_diff_pct': float(results['Diff_Pct'].max()) if results['Diff_Pct'].notna().any() else None,
        'checked_at': datetime.now().isoformat(timespec='seconds'),
    }


def print_report(results: pd.DataFrame) -> None:
    summary = summarize(results)
    print("\n" + "=" * 90)
    print("BHAVCOPY RELIABILITY CHECK (vs broker) - validation gate before touching discovery.py")
    print("=" * 90)
    print(f"Checked: {summary['checked_at']}")
    print(f"Total (symbol, date) checks: {summary['total_checks']}")
    print(f"  OK: {summary.get('ok', 0)}")
    print(f"  MATERIAL_DIFF (>{MATERIAL_DISCREPANCY_PCT}%): {summary.get('material_diff', 0)}")
    print(f"  MISSING_BHAVCOPY: {summary.get('missing_bhavcopy', 0)}")
    print(f"  MISSING_BROKER: {summary.get('missing_broker', 0)}")
    if summary.get('max_diff_pct') is not None:
        print(f"  Max close-price diff seen: {summary['max_diff_pct']:.3f}%")

    problems = results[results['Flag'] != 'OK']
    if not problems.empty:
        print(f"\n--- Flagged rows ({len(problems)}) ---")
        print(problems.to_string(index=False))
    else:
        print("\nNo discrepancies - clean run.")
    print("=" * 90)
    print("Re-run this across several sessions before considering the discovery.py (V1.0) swap.")

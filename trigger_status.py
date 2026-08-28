"""On-demand freshness check for an existing EMFB/momentum report's trigger levels.

Every emfb_report_*.csv / momentum_report_*.csv is a snapshot: Trigger/Stop/
Target are only ever evaluated against the price at scan time. Nothing in
the pipeline re-checks them later, so a stale report reads exactly like a
fresh one - the 2026-08-06 session (CGPOWER, SYRMA, NETWEB all breaking
their trigger then fading back below it within two days) only got caught by
manually pulling live candles for each symbol one at a time.

This module automates that same check: given a report file, it pulls daily
candles for every symbol since the report's own timestamp (via
DataBroker.fetch_daily_candles - no changes to data_broker.py) and
classifies what's actually happened since.

Daily-granularity limitation: stop-vs-target ordering on the same calendar
day can't be determined from daily OHLC alone (no intraday sequencing) - if
a single day's low undercuts the stop AND its high clears the target, this
treats it as STOPPED_OUT (the more conservative read for risk purposes).
"""
import glob
import logging
import os
from datetime import datetime
from typing import Optional

import pandas as pd

from data_broker import DataBroker

logger = logging.getLogger(__name__)

REPORT_GLOBS = {
    'emfb': 'emfb_report_*.csv',
    'momentum': 'momentum_report_*.csv',
}


def find_latest_report(report_type: str) -> Optional[str]:
    """Returns the path of the most recently generated report of the given
    type ('emfb' or 'momentum'), or None if none exist."""
    pattern = REPORT_GLOBS.get(report_type)
    if pattern is None:
        raise ValueError(f"Unknown report_type '{report_type}'; expected one of {list(REPORT_GLOBS)}")
    matches = glob.glob(pattern)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def classify_trigger_status(trigger: float, stop: float, target: float, candles: pd.DataFrame) -> dict:
    """candles: daily OHLC rows strictly after the report's own date, sorted
    ascending by timestamp. Returns the fields merged into each report row.

    Public (not module-private) because performance_tracker.py reuses this
    exact classification logic for its win/loss scorecard - a signal's
    outcome must be judged identically whether you're checking one report's
    freshness today or aggregating every report's historical outcomes.
    """
    if candles.empty:
        return {
            'Trigger_Status': 'NO_NEW_DATA',
            'Latest_Close': None,
            'Pct_From_Trigger': None,
        }

    stop_hits = candles[candles['low'] <= stop]
    target_hits = candles[candles['high'] >= target]
    stop_hit_date = stop_hits.index.min() if not stop_hits.empty else None
    target_hit_date = target_hits.index.min() if not target_hits.empty else None

    latest_close = float(candles['close'].iloc[-1])
    pct_from_trigger = ((latest_close - trigger) / trigger) * 100 if trigger else None

    if stop_hit_date is not None and (target_hit_date is None or stop_hit_date <= target_hit_date):
        status = 'STOPPED_OUT'
    elif target_hit_date is not None:
        status = 'HIT_TARGET'
    else:
        breach = candles[candles['high'] >= trigger]
        if breach.empty:
            status = 'NOT_YET_TRIGGERED'
        else:
            status = 'EXTENDED' if latest_close >= trigger else 'FADED'

    return {
        'Trigger_Status': status,
        'Latest_Close': round(latest_close, 2),
        'Pct_From_Trigger': round(pct_from_trigger, 2) if pct_from_trigger is not None else None,
    }


def check_trigger_status(report_path: str, broker: Optional[DataBroker] = None) -> pd.DataFrame:
    """Loads `report_path`, checks each row's Trigger/Stop/Target against
    daily candles since the report's own timestamp, and returns the report
    DataFrame with Trigger_Status/Latest_Close/Pct_From_Trigger/
    Days_Since_Report columns added. Also saves an annotated copy next to
    the original (suffix '_status.csv') - the original report is never
    modified.
    """
    df = pd.read_csv(report_path)
    required = {'Symbol', 'Trigger', 'Stop', 'Target', 'timestamp'}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{report_path} is missing required columns: {missing}")

    owns_broker = broker is None
    broker = broker if broker is not None else DataBroker()

    report_dates = pd.to_datetime(df['timestamp']).dt.tz_localize(None).dt.normalize()
    today = pd.Timestamp(datetime.now().date())

    statuses = []
    for i, row in df.iterrows():
        report_date = report_dates.iloc[i]
        days_since = (today - report_date).days
        days_back = max(days_since + 5, 10)  # small buffer for weekends/holidays

        candles = broker.fetch_daily_candles(row['Symbol'], days_back=days_back)
        if not candles.empty:
            candle_dates = candles.index.tz_localize(None) if candles.index.tz is not None else candles.index
            candles = candles.set_axis(candle_dates)
            candles = candles[candles.index.normalize() > report_date].sort_index()

        result = classify_trigger_status(float(row['Trigger']), float(row['Stop']), float(row['Target']), candles)
        result['Days_Since_Report'] = days_since
        statuses.append(result)

    status_df = pd.DataFrame(statuses)
    annotated = pd.concat([df.reset_index(drop=True), status_df], axis=1)

    base, ext = os.path.splitext(report_path)
    out_path = f"{base}_status{ext}"
    try:
        annotated.to_csv(out_path, index=False)
        logger.info(f"Trigger-status annotated report saved to {out_path}")
    except OSError as e:
        logger.warning(f"Failed to save annotated report: {e}")

    if owns_broker:
        broker.close_session()

    return annotated


def print_trigger_status(annotated: pd.DataFrame, top_n: int = 20) -> None:
    display_cols = [
        'Symbol', 'Trigger', 'Stop', 'Target', 'Latest_Close',
        'Pct_From_Trigger', 'Trigger_Status', 'Days_Since_Report'
    ]
    cols = [c for c in display_cols if c in annotated.columns]
    print("\n" + "=" * 100)
    print("TRIGGER FRESHNESS CHECK")
    print("=" * 100)
    print(annotated[cols].head(top_n).to_string(index=False))
    print("=" * 100)

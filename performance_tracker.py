"""Closes the feedback loop: did this system's own past picks actually work?

trigger_status.py answers "is this one report still fresh" for the latest
scan. This module asks the broader question across every report on disk:
of all the signals EMFB/momentum ever produced, what fraction that reached
a resolved outcome (hit target or got stopped out) were actually winners?
Without this, there's no way to tell whether the scoring weights in
config.EMFB.WEIGHT_PROFILES are actually working versus just producing
plausible-looking noise.

Built entirely from symbol_history.py's report aggregation (every report
row, one per (report, symbol) appearance) and trigger_status.py's outcome
classification (same function, reused verbatim - a signal's win/loss must
be judged identically whether checking one report's freshness or scoring
the whole track record). No changes to either module beyond making
trigger_status's classifier public.

FADED/EXTENDED/NOT_YET_TRIGGERED are not losses or wins - they mean the
signal is either still open or never triggered at all, so win rate is
computed only over the resolved subset (STOPPED_OUT + HIT_TARGET). A
signal produced very recently (e.g. today's scan) will almost always be
unresolved; this scorecard is only meaningful once enough time has passed
for outcomes to actually play out.
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

import pandas as pd

from data_broker import DataBroker
from symbol_history import build_history
from trigger_status import classify_trigger_status

logger = logging.getLogger(__name__)

RESOLVED_STATUSES = {'STOPPED_OUT', 'HIT_TARGET'}
TIER_PERFORMANCE_CACHE_PATH = 'tier_performance.json'


def build_scorecard(report_type: str, broker: Optional[DataBroker] = None) -> pd.DataFrame:
    """One row per historical (report, symbol) signal, with its outcome as
    of today. Fetches each distinct symbol's daily candles once (not once
    per report appearance) to keep API calls down - across every report on
    disk, the same symbol often reappears multiple times.
    """
    history = build_history(report_type)
    if history.empty:
        return history

    owns_broker = broker is None
    broker = broker if broker is not None else DataBroker()

    today = pd.Timestamp.now().normalize()
    candle_cache: dict[str, pd.DataFrame] = {}

    outcomes = []
    for _, row in history.iterrows():
        symbol = row['Symbol']
        report_date = pd.Timestamp(row['timestamp']).tz_localize(None).normalize()
        days_since = max((today - report_date).days, 0)

        if symbol not in candle_cache:
            # Fetch enough history to cover this symbol's *earliest* report
            # appearance, then reuse the same frame for every later
            # appearance of the same symbol (sliced per-row below).
            earliest_for_symbol = history.loc[history['Symbol'] == symbol, 'timestamp'].min()
            earliest_days_since = max((today - pd.Timestamp(earliest_for_symbol).tz_localize(None).normalize()).days, 0)
            candle_cache[symbol] = broker.fetch_daily_candles(symbol, days_back=earliest_days_since + 10)

        candles = candle_cache[symbol]
        if not candles.empty:
            candle_dates = candles.index.tz_localize(None) if candles.index.tz is not None else candles.index
            candles = candles.set_axis(candle_dates)
            candles = candles[candles.index.normalize() > report_date].sort_index()

        result = classify_trigger_status(float(row['Trigger']), float(row['Stop']), float(row['Target']), candles)
        result['Symbol'] = symbol
        result['Confidence'] = row['Confidence']
        result['Report_File'] = row['Report_File']
        result['timestamp'] = row['timestamp']
        result['Days_Since_Report'] = days_since
        outcomes.append(result)

    if owns_broker:
        broker.close_session()

    return pd.DataFrame(outcomes)


def summarize_scorecard(scorecard: pd.DataFrame) -> dict:
    """Aggregate win rate overall and by Confidence tier, over resolved
    signals only (STOPPED_OUT/HIT_TARGET) - FADED/EXTENDED/NOT_YET_TRIGGERED
    aren't outcomes yet."""
    if scorecard.empty:
        return {'overall': {}, 'by_confidence': pd.DataFrame()}

    resolved = scorecard[scorecard['Trigger_Status'].isin(RESOLVED_STATUSES)]

    def _win_rate(df: pd.DataFrame) -> dict:
        wins = (df['Trigger_Status'] == 'HIT_TARGET').sum()
        losses = (df['Trigger_Status'] == 'STOPPED_OUT').sum()
        total = wins + losses
        return {
            'Resolved': total,
            'Wins': wins,
            'Losses': losses,
            'Win_Rate_Pct': round((wins / total) * 100, 1) if total else None,
        }

    overall = _win_rate(resolved)
    overall['Total_Signals'] = len(scorecard)
    overall['Still_Open'] = len(scorecard) - len(resolved)

    by_confidence = (
        resolved.groupby('Confidence', dropna=False)
        .apply(lambda g: pd.Series(_win_rate(g)), include_groups=False)
        .reset_index()
        if not resolved.empty else pd.DataFrame()
    )

    return {'overall': overall, 'by_confidence': by_confidence}


def save_tier_performance(report_type: str, summary: dict) -> None:
    """Persists by-Confidence-tier win rates to TIER_PERFORMANCE_CACHE_PATH so
    signal_quality.py can read real historical performance without re-running
    the full (expensive, ~1 API call per distinct symbol) scorecard build on
    every report check. Keyed by report_type so emfb/momentum track separately
    - their scoring engines are shared, but their signal sets and regime
    gating differ enough that pooling them would blur which one a given win
    rate actually describes.
    """
    by_confidence = summary.get('by_confidence')
    if by_confidence is None or by_confidence.empty:
        logger.warning(f"No resolved signals to persist tier performance for '{report_type}'.")
        return

    payload = {}
    if os.path.exists(TIER_PERFORMANCE_CACHE_PATH):
        try:
            with open(TIER_PERFORMANCE_CACHE_PATH, 'r') as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to read existing {TIER_PERFORMANCE_CACHE_PATH}, overwriting: {e}")
            payload = {}

    payload[report_type] = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'overall': summary['overall'],
        'by_confidence': by_confidence.to_dict(orient='records'),
    }

    try:
        with open(TIER_PERFORMANCE_CACHE_PATH, 'w') as f:
            json.dump(payload, f, indent=2, default=str)
        logger.info(f"Tier performance for '{report_type}' saved to {TIER_PERFORMANCE_CACHE_PATH}")
    except OSError as e:
        logger.warning(f"Failed to save {TIER_PERFORMANCE_CACHE_PATH}: {e}")


def load_tier_performance(report_type: str) -> Optional[dict]:
    """Reads back what save_tier_performance wrote. None if no cache exists
    yet or nothing was saved for this report_type - callers must treat that
    as 'run `python main.py scorecard` first', not as 'zero signals work'."""
    if not os.path.exists(TIER_PERFORMANCE_CACHE_PATH):
        return None
    try:
        with open(TIER_PERFORMANCE_CACHE_PATH, 'r') as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to read {TIER_PERFORMANCE_CACHE_PATH}: {e}")
        return None
    return payload.get(report_type)


def print_scorecard(scorecard: pd.DataFrame, summary: dict) -> None:
    print("\n" + "=" * 100)
    print("PERFORMANCE SCORECARD")
    print("=" * 100)

    overall = summary['overall']
    if not overall:
        print("No historical reports to score yet.")
        print("=" * 100)
        return

    print(
        f"Total signals: {overall['Total_Signals']} | Still open: {overall['Still_Open']} | "
        f"Resolved: {overall['Resolved']} (Wins: {overall['Wins']}, Losses: {overall['Losses']}) | "
        f"Win rate: {overall['Win_Rate_Pct']}%"
    )

    by_conf = summary['by_confidence']
    if not by_conf.empty:
        print("\nBy Confidence tier:")
        print(by_conf.to_string(index=False))

    print("\nPer-signal detail:")
    cols = ['Symbol', 'Confidence', 'timestamp', 'Trigger_Status', 'Pct_From_Trigger', 'Days_Since_Report']
    cols = [c for c in cols if c in scorecard.columns]
    print(scorecard[cols].sort_values('timestamp').to_string(index=False))
    print("=" * 100)

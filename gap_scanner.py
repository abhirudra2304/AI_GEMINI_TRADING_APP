"""Live gap-up/gap-down scanner - today's open vs prior close, ranked.

Distinct from the existing `main.py gap` strategy: that `gap` is an
entry-timing/holding-period label for BTST-family execution (next-session
entry, session-close exit) applied to the same historical breakout
screening as BTST - it never actually computes today's open-vs-prev-close
%. This module answers the literal question "which stocks in the scan
universe gapped up/down at today's open," ranked by gap size. Flagged
missing during the 2026-08-25 architecture audit; built same day.

Reuses the exact daily-candle fetch the rest of the app already relies on
(`emfb.py`'s `_fetch_data_for_universe` / `TIMEFRAME_FETCH_SPECS['daily']`,
`(ONE_DAY, 400)`) so this shares a cache with EMFB/momentum/Discovery
instead of paying for its own separate fetch when one of those already
ran today. On a fully cold cache (no other scan run yet today) this still
costs ~1 API call per symbol at the shared 1.8s rate-limit spacing - for
the curated ~219-symbol universe, that's roughly 6-7 minutes, which is why
this is meant to run once near 09:15 rather than continuously.

Only meaningful near the open. Today's daily candle's Open is a live
print - there's no bhavcopy substitute for it (NSE's EOD bhavcopy
publishes after close, too late for an at-open decision). Before the
market opens, no symbol has a same-day daily candle yet, so every symbol
reads as stale and the report is correctly empty rather than wrong. The
longer after 09:15 this runs, the more `Last` will have drifted from
`Open` - see Move_From_Open_Pct, which exists specifically to show
whether a gap is holding or already fading.
"""
import logging
import os
from datetime import datetime
from typing import Optional

import pandas as pd
import yaml

import config
from data_broker import DataBroker
from provider import ConstituentProvider
from emfb import _fetch_data_for_universe

logger = logging.getLogger(__name__)

GAP_CONFIG_PATH = "gap_scanner_config.yaml"
GAP_OUTPUT_PATH = "gap_scan_candidates.csv"

_DEFAULT_CONFIG = {
    'min_gap_pct': 2.0,
    'report_top_n': 25,
}


def _load_config() -> dict:
    if not os.path.exists(GAP_CONFIG_PATH):
        logger.warning(f"{GAP_CONFIG_PATH} not found; using built-in gap-scan defaults.")
        return _DEFAULT_CONFIG
    try:
        with open(GAP_CONFIG_PATH, 'r') as f:
            loaded = yaml.safe_load(f)
        if not isinstance(loaded, dict):
            raise ValueError("top-level YAML content is not a mapping")
        return {**_DEFAULT_CONFIG, **loaded}
    except Exception as e:
        logger.warning(f"Failed to parse {GAP_CONFIG_PATH} ({e}); using built-in defaults.")
        return _DEFAULT_CONFIG


def detect_gaps(broker: Optional[DataBroker] = None, cfg: Optional[dict] = None) -> pd.DataFrame:
    """Curated-universe scan: each symbol's today-open vs prior-close %,
    using the same daily candle every other scan in this app fetches.
    Returns rows clearing min_gap_pct, sorted gap-up first. Empty (with
    df.attrs['market_not_open']=True) if no symbol has a same-day candle
    yet - i.e. before 09:15 or on a non-trading day."""
    cfg = cfg if cfg is not None else _load_config()
    broker = broker if broker is not None else DataBroker()

    provider = ConstituentProvider(index_name=config.EMFB.UNIVERSE_INDEX)
    universe = provider.get_universe()
    symbols = [s['symbol'] for s in universe]

    data_store = _fetch_data_for_universe(broker, symbols, ['daily'])

    today = datetime.now(config.MARKET_TZ).date()
    rows = []
    stale_count = 0

    for symbol in symbols:
        df = data_store.get(symbol, {}).get('daily')
        if df is None or len(df) < 2:
            continue
        last_ts = df['Timestamp'].iloc[-1]
        last_date = last_ts.date() if hasattr(last_ts, 'date') else pd.Timestamp(last_ts).date()
        if last_date != today:
            stale_count += 1
            continue

        open_today = float(df['Open'].iloc[-1])
        prev_close = float(df['Close'].iloc[-2])
        last_price = float(df['Close'].iloc[-1])
        if prev_close <= 0 or open_today <= 0:
            continue

        gap_pct = ((open_today - prev_close) / prev_close) * 100
        move_from_open_pct = ((last_price - open_today) / open_today) * 100 if open_today > 0 else 0.0

        if abs(gap_pct) >= cfg['min_gap_pct']:
            rows.append({
                'Symbol': symbol,
                'Gap_Pct': round(gap_pct, 2),
                'Open': round(open_today, 2),
                'Prev_Close': round(prev_close, 2),
                'Last': round(last_price, 2),
                'Move_From_Open_Pct': round(move_from_open_pct, 2),
            })

    result = pd.DataFrame(rows)
    result.attrs['market_not_open'] = (stale_count == len(symbols)) and len(symbols) > 0
    result.attrs['as_of'] = datetime.now(config.MARKET_TZ).isoformat(timespec='seconds')

    if result.empty:
        return result
    return result.sort_values('Gap_Pct', ascending=False).head(cfg['report_top_n']).reset_index(drop=True)


def build_gap_report(broker: Optional[DataBroker] = None) -> pd.DataFrame:
    """End-to-end: fetch -> detect -> save. Returns the DataFrame."""
    cfg = _load_config()
    gaps = detect_gaps(broker, cfg)

    if not gaps.empty:
        try:
            gaps.to_csv(GAP_OUTPUT_PATH, index=False)
        except OSError as e:
            logger.warning(f"Failed to save {GAP_OUTPUT_PATH}: {e}")

    return gaps


def print_gap_report(df: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print(f"GAP SCAN - today's open vs prior close, {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 100)

    if df.empty:
        if df.attrs.get('market_not_open'):
            print("No same-day candles yet - market hasn't opened, or this ran before the first candle formed.")
        else:
            print("No symbols cleared the gap threshold today.")
        print("=" * 100)
        return

    cols = ['Symbol', 'Gap_Pct', 'Open', 'Prev_Close', 'Last', 'Move_From_Open_Pct']
    gap_up = df[df['Gap_Pct'] > 0]
    gap_down = df[df['Gap_Pct'] < 0]

    if not gap_up.empty:
        print(f"\n--- GAP UP ({len(gap_up)}) ---")
        print(gap_up[cols].to_string(index=False))

    if not gap_down.empty:
        print(f"\n--- GAP DOWN ({len(gap_down)}) ---")
        print(gap_down[cols].to_string(index=False))

    print("\n(Move_From_Open_Pct shows whether the gap is holding or already fading since open.)")
    print("=" * 100)
    print(f"Full table saved to {GAP_OUTPUT_PATH}.")

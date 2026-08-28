"""Dynamic Overextension Penalty Overlay for BTST/SWING/EMFB signals.

Calibrates extension thresholds dynamically using:
1. India VIX Regime Scaling: Adjusts base % distance ceilings up/down based on
   broad market volatility (tightens in chop/low-VIX, expands in momentum/high-VIX).
2. Stock-Specific ATR Multiples: Normalizes distance above EMA20 by the stock's
   own 14-day Average True Range (ATR), preventing high-beta runners from being
   penalized prematurely while catching overextended low-beta grinders.
"""
import logging
import math
import os
from typing import Optional, Dict, Any

import numpy as np
import pandas as pd
import yaml

from data_broker import DataBroker

logger = logging.getLogger(__name__)

EXTENSION_CONFIG_PATH = 'extension_config.yaml'
_DEFAULT_CONFIG = {
    'normal_max_pct': 8.0,
    'very_extended_min_pct': 15.0,
    'normal_max_atr_mult': 2.5,
    'very_extended_min_atr_mult': 4.0,
    'enable_vix_scaling': True,
    'vix_baseline': 15.0,
    'vix_min_scale': 0.75,
    'vix_max_scale': 1.35,
    'ema_lookback_days': 300,
}


def _load_config() -> dict:
    if not os.path.exists(EXTENSION_CONFIG_PATH):
        logger.warning(f"{EXTENSION_CONFIG_PATH} not found; using built-in extension-score defaults.")
        return _DEFAULT_CONFIG
    try:
        with open(EXTENSION_CONFIG_PATH, 'r') as f:
            loaded = yaml.safe_load(f)
        if not isinstance(loaded, dict):
            raise ValueError("top-level YAML content is not a mapping")
        return {**_DEFAULT_CONFIG, **loaded}
    except Exception as e:
        logger.warning(f"Failed to parse {EXTENSION_CONFIG_PATH} ({e}); using built-in defaults.")
        return _DEFAULT_CONFIG


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _compute_atr(candles: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Calculates 14-period Average True Range (ATR) from daily OHLCV."""
    if candles.empty or len(candles) < period:
        return None

    high = candles['high'] if 'high' in candles.columns else candles.get('High')
    low = candles['low'] if 'low' in candles.columns else candles.get('Low')
    close = candles['close'] if 'close' in candles.columns else candles.get('Close')

    if high is None or low is None or close is None:
        return None

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr_series = tr.ewm(span=period, adjust=False).mean()
    val = atr_series.iloc[-1]
    return float(val) if pd.notna(val) and val > 0 else None


def _get_point_in_time_vix(broker: DataBroker, as_of_date: pd.Timestamp, vix_cache: Dict[str, Any]) -> float:
    """Fetches point-in-time India VIX close on or before as_of_date."""
    if 'vix_df' not in vix_cache:
        try:
            vix_df = broker.fetch_ohlcv("India VIX", "ONE_DAY", 250, caller="ExtensionScore")
            if vix_df is not None and not vix_df.empty:
                vix_dates = vix_df.index.tz_localize(None) if vix_df.index.tz is not None else vix_df.index
                vix_df = vix_df.set_axis(vix_dates).sort_index()
                vix_cache['vix_df'] = vix_df
            else:
                vix_cache['vix_df'] = pd.DataFrame()
        except Exception as e:
            logger.debug(f"Could not fetch India VIX: {e}")
            vix_cache['vix_df'] = pd.DataFrame()

    vix_df = vix_cache.get('vix_df')
    if vix_df is None or vix_df.empty:
        return 15.0  # Neutral baseline

    as_of = pd.Timestamp(as_of_date).normalize()
    if as_of.tzinfo is not None:
        as_of = as_of.tz_localize(None)

    hist = vix_df[vix_df.index.normalize() <= as_of]
    if hist.empty:
        return 15.0

    close_col = 'Close' if 'Close' in hist.columns else 'close'
    vix_val = hist[close_col].iloc[-1]
    return float(vix_val) if pd.notna(vix_val) and vix_val > 0 else 15.0


def compute_extension(
    symbol: str,
    as_of_date: pd.Timestamp,
    broker: DataBroker,
    cfg: dict,
    vix_cache: Optional[dict] = None,
) -> dict:
    """Computes dynamic volatility-adjusted EMA20 distance and ATR multiples
    as of `as_of_date` using only point-in-time data."""
    today = pd.Timestamp.now().normalize()
    as_of_date = pd.Timestamp(as_of_date)
    if as_of_date.tzinfo is not None:
        as_of_date = as_of_date.tz_localize(None)
    as_of_date = as_of_date.normalize()
    days_back = max((today - as_of_date).days, 0) + cfg['ema_lookback_days']

    candles = broker.fetch_daily_candles(symbol, days_back=days_back)
    if candles.empty:
        return {
            'EMA20_Distance_Pct': None,
            'ATR_Distance_Mult': None,
            'Dynamic_Normal_Max': cfg['normal_max_pct'],
            'India_VIX': 15.0,
            'Extension_Flag': 'NO_DATA',
        }

    candle_dates = candles.index.tz_localize(None) if candles.index.tz is not None else candles.index
    candles = candles.set_axis(candle_dates).sort_index()
    candles = candles[candles.index.normalize() <= as_of_date]

    if len(candles) < 20:
        return {
            'EMA20_Distance_Pct': None,
            'ATR_Distance_Mult': None,
            'Dynamic_Normal_Max': cfg['normal_max_pct'],
            'India_VIX': 15.0,
            'Extension_Flag': 'INSUFFICIENT_HISTORY',
        }

    close_col = 'close' if 'close' in candles.columns else 'Close'
    close = float(candles[close_col].iloc[-1])
    ema20 = float(_ema(candles[close_col], 20).iloc[-1])
    pct_above_ema20 = ((close - ema20) / ema20) * 100 if ema20 else None

    # 1. Stock-Level Volatility: ATR 14 calculation
    atr14 = _compute_atr(candles, period=14)
    atr_mult = ((close - ema20) / atr14) if (atr14 and atr14 > 0) else None

    # 2. Market-Level Volatility: India VIX scaling
    vix_cache = vix_cache if vix_cache is not None else {}
    vix_val = _get_point_in_time_vix(broker, as_of_date, vix_cache)

    if cfg.get('enable_vix_scaling', True) and vix_val:
        baseline = float(cfg.get('vix_baseline', 15.0))
        raw_scale = (vix_val / baseline) ** 0.5
        vix_scale = max(float(cfg.get('vix_min_scale', 0.75)), min(float(cfg.get('vix_max_scale', 1.35)), raw_scale))
    else:
        vix_scale = 1.0

    dynamic_normal_pct = round(cfg['normal_max_pct'] * vix_scale, 2)
    dynamic_very_extended_pct = round(cfg['very_extended_min_pct'] * vix_scale, 2)
    normal_max_atr = float(cfg.get('normal_max_atr_mult', 2.5))
    very_extended_min_atr = float(cfg.get('very_extended_min_atr_mult', 4.0))

    # 3. Hybrid Volatility-Adaptive Evaluation
    if pct_above_ema20 is None:
        flag = 'NO_DATA'
    elif (pct_above_ema20 <= dynamic_normal_pct) or (atr_mult is not None and atr_mult <= normal_max_atr):
        flag = 'NORMAL'
    elif (pct_above_ema20 >= dynamic_very_extended_pct) or (atr_mult is not None and atr_mult >= very_extended_min_atr):
        flag = 'VERY_EXTENDED'
    else:
        flag = 'EXTENDED'

    return {
        'EMA20_Distance_Pct': round(pct_above_ema20, 2) if pct_above_ema20 is not None else None,
        'ATR_Distance_Mult': round(atr_mult, 2) if atr_mult is not None else None,
        'Dynamic_Normal_Max': dynamic_normal_pct,
        'India_VIX': round(vix_val, 2),
        'Extension_Flag': flag,
    }


def annotate_extension(report_df: pd.DataFrame, broker: Optional[DataBroker] = None) -> pd.DataFrame:
    """Adds EMA20_Distance_Pct, ATR_Distance_Mult, India_VIX, and Extension_Flag
    columns to a report DataFrame."""
    df = report_df.copy()
    if df.empty or 'Symbol' not in df.columns or 'timestamp' not in df.columns:
        return df

    cfg = _load_config()
    owns_broker = broker is None
    broker = broker if broker is not None else DataBroker()
    vix_cache = {}

    results = []
    for _, row in df.iterrows():
        result = compute_extension(row['Symbol'], pd.Timestamp(row['timestamp']), broker, cfg, vix_cache=vix_cache)
        results.append(result)

    ext_df = pd.DataFrame(results)
    df = pd.concat([df.reset_index(drop=True), ext_df], axis=1)

    if owns_broker:
        broker.close_session()

    return df


def print_extension_report(df: pd.DataFrame, top_n: int = 20) -> None:
    if df.empty or 'Extension_Flag' not in df.columns:
        print("\nNo signals to show.")
        return

    display_cols = [c for c in ['Symbol', 'EMFB_Score', 'Confidence', 'EMA20_Distance_Pct',
                                 'ATR_Distance_Mult', 'Dynamic_Normal_Max', 'India_VIX',
                                 'Extension_Flag', 'Trigger', 'Stop', 'Target'] if c in df.columns]

    vix_note = f" (India VIX Ref: {df['India_VIX'].iloc[0]:.1f})" if 'India_VIX' in df.columns and not df.empty else ""
    print("\n" + "=" * 110)
    print(f"DYNAMIC VOLATILITY EXTENSION CHECK{vix_note}")
    print("=" * 110)
    for flag in ['NORMAL', 'EXTENDED', 'VERY_EXTENDED', 'INSUFFICIENT_HISTORY', 'NO_DATA']:
        subset = df[df['Extension_Flag'] == flag]
        if subset.empty:
            continue
        print(f"\n{flag} ({len(subset)}):")
        print(subset[display_cols].head(top_n).to_string(index=False))
    print("=" * 110)
    print("Note: Extension is calibrated dynamically against India VIX and stock-specific ATR multiples.")


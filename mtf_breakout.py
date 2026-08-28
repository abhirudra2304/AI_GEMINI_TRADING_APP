"""
Multi-Timeframe (MTF) breakout detection.

Bridges the daily-EOD scanner's macro context (RSI/ADX/SuperTrend, resistance)
with a live 15-minute micro trigger, so a stock that is "Watch only" on the
daily close but breaking out intraday on volume gets promoted to an
actionable BUY with a stop tightened to the 15m SuperTrend instead of the
wide daily one.
"""
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Any, Dict, Optional

from utils import install_and_import

# Reuse the same lazy-install pattern as scanner_engine.py so this module
# degrades gracefully (returns NaNs, not a crash) if pandas-ta is missing.
ta = install_and_import('pandas-ta', critical=False)

# Tunable thresholds pulled out as constants so they can be wired into
# config.py later without touching the logic below.
MACRO_RESISTANCE_LOOKBACK = 20      # days
MICRO_VOLUME_MA_PERIOD = 20         # 15m bars
BREAKOUT_VOLUME_RATIO_MIN = 2.5
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0         # pandas-ta "(10, 3)" SuperTrend


def get_target_candle_index(df_15m: pd.DataFrame, now: Optional[datetime] = None) -> int:
    """
    Fractional Candle Guard: picks which row of a 15m frame is safe to read as
    "the current candle."

    A new 15-minute row starts printing the instant a boundary (:00, :15, :30,
    :45) is crossed, but its Volume is still accumulating and understates the
    true bar volume for as long as the broker takes to finish streaming it. If
    a scan happens to run in that window, reading iloc[-1] directly corrupts
    vol_ratio_15m with an artificially low denominator/numerator. During the
    first 60 seconds after a boundary, step back to the last fully closed
    candle (iloc[-2]) instead; otherwise iloc[-1] is safe to use as-is.

    Args:
        df_15m: 15-minute OHLCV frame, sorted ascending, most-recent bar last.
        now:    Wall-clock time to evaluate against. Defaults to datetime.now();
                exposed as a parameter purely so this is deterministically
                testable without patching the clock.

    Returns:
        -2 if within the first 60s of a 15-min boundary AND a prior candle
        exists, otherwise -1. Never raises on a 1-row frame — falls back to -1.
    """
    if now is None:
        now = datetime.now()

    seconds_into_bucket = (now.minute % 15) * 60 + now.second
    is_fresh_boundary = seconds_into_bucket < 60

    if is_fresh_boundary and len(df_15m) >= 2:
        return -2
    return -1


def _normalize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    """pandas-ta expects lowercase OHLCV column names."""
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    return out


def _latest_supertrend_line(df_norm: pd.DataFrame, period: int, multiplier: float) -> pd.Series:
    """
    Runs pandas-ta SuperTrend on an already-lowercased OHLC frame and returns
    the trend line (SUPERT_<period>_<multiplier>) as a plain Series, or an
    all-NaN series if pandas-ta is unavailable / insufficient data.
    """
    if ta is None or len(df_norm) < period:
        return pd.Series(np.nan, index=df_norm.index)

    st = ta.supertrend(df_norm['high'], df_norm['low'], df_norm['close'],
                        length=period, multiplier=multiplier)
    if st is None or st.empty:
        return pd.Series(np.nan, index=df_norm.index)

    # First column of the result is always the trend line itself,
    # e.g. 'SUPERT_10_3.0' — direction/bands are the other columns.
    return st.iloc[:, 0]


def analyze_mtf_breakout(df_daily: pd.DataFrame, df_15m: pd.DataFrame,
                          now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Combines daily macro context with a 15m intraday trigger into one signal.

    Args:
        df_daily: Daily OHLCV, sorted ascending by time, most-recent bar last.
        df_15m:   15-minute OHLCV, sorted ascending by time, most-recent bar last.
        now:      Wall-clock time for the Fractional Candle Guard (see
                  get_target_candle_index). Defaults to datetime.now().

    Returns:
        dict with Decision, Breakout_Status, Stop_Loss, Vol_Ratio_15m, LTP,
        plus the underlying macro/micro readings for downstream logging.
    """
    empty_result: Dict[str, Any] = {
        'Decision': 'NO DATA',
        'Breakout_Status': 'NO',
        'Stop_Loss': np.nan,
        'Vol_Ratio_15m': 0.0,
        'LTP': np.nan,
    }
    if df_daily is None or df_15m is None or df_daily.empty or df_15m.empty:
        return empty_result
    if len(df_daily) < MACRO_RESISTANCE_LOOKBACK + 1 or len(df_15m) < MICRO_VOLUME_MA_PERIOD + 1:
        return empty_result

    # =====================================================================
    # 1. MACRO CONTEXT (daily) — trend quality + the level a breakout must clear
    # =====================================================================
    daily = _normalize_ohlcv_columns(df_daily)

    daily_rsi = ta.rsi(daily['close'], length=14) if ta is not None else pd.Series(dtype=float)
    daily_adx_df = ta.adx(daily['high'], daily['low'], daily['close'], length=14) if ta is not None else None
    # pandas-ta names the ADX column 'ADX_14' regardless of case of the OHLC input.
    daily_adx = daily_adx_df['ADX_14'] if daily_adx_df is not None and 'ADX_14' in daily_adx_df.columns else pd.Series(dtype=float)
    daily_supertrend = _latest_supertrend_line(daily, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)

    # shift(1): resistance is measured from the last N *completed* days,
    # excluding today's still-forming daily bar. Without the shift, a stock
    # would need to close above its own current-day high to "break out" —
    # a self-referential, near-impossible condition.
    macro_resistance = daily['high'].shift(1).rolling(window=MACRO_RESISTANCE_LOOKBACK).max().iloc[-1]

    rsi_val = float(daily_rsi.iloc[-1]) if not daily_rsi.empty and pd.notna(daily_rsi.iloc[-1]) else np.nan
    adx_val = float(daily_adx.iloc[-1]) if not daily_adx.empty and pd.notna(daily_adx.iloc[-1]) else np.nan
    daily_stop = float(daily_supertrend.iloc[-1]) if pd.notna(daily_supertrend.iloc[-1]) else np.nan

    # =====================================================================
    # 2. MICRO TRIGGER (15m) — is price actually breaking out right now, on volume?
    # =====================================================================
    micro = _normalize_ohlcv_columns(df_15m)

    # Fractional Candle Guard: if we're in the first 60s of a new 15m boundary,
    # the last row's volume is still filling in — read the last *closed* bar
    # instead. Supertrend/volume-MA are still computed over the full series
    # (both are causal/rolling, so untouched by whatever the freshest row holds);
    # only the *position* we read "current" values from shifts.
    target_idx = get_target_candle_index(df_15m, now=now)

    micro_supertrend = _latest_supertrend_line(micro, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)
    volume_ma_20 = micro['volume'].rolling(window=MICRO_VOLUME_MA_PERIOD).mean()

    ltp = float(micro['close'].iloc[target_idx])
    intraday_stop = float(micro_supertrend.iloc[target_idx]) if pd.notna(micro_supertrend.iloc[target_idx]) else np.nan

    current_vol = float(micro['volume'].iloc[target_idx])
    baseline_vol = float(volume_ma_20.iloc[target_idx]) if pd.notna(volume_ma_20.iloc[target_idx]) and volume_ma_20.iloc[target_idx] > 0 else np.nan

    # Pro-rate the baseline by how much of the current 15m bucket has actually
    # elapsed, so a forming candle isn't penalized against a full-bar average.
    # Only applies when reading the still-forming candle (target_idx == -1) —
    # if the Fractional Candle Guard stepped back to the last closed bar
    # (target_idx == -2), that bar is complete and needs no proration.
    # Floored at 30s to avoid an inflated ratio right at a fresh boundary.
    if target_idx == -1:
        eval_time = now if now is not None else datetime.now()
        seconds_elapsed = max(30, (eval_time.minute % 15) * 60 + eval_time.second)
    else:
        seconds_elapsed = 900
    baseline_vol_prorated = baseline_vol * (seconds_elapsed / 900.0) if pd.notna(baseline_vol) else np.nan

    vol_ratio_15m = (current_vol / baseline_vol_prorated) if pd.notna(baseline_vol_prorated) and baseline_vol_prorated > 0 else 0.0

    # =====================================================================
    # 3. DYNAMIC BREAKOUT LOGIC
    # =====================================================================
    price_above_resistance = pd.notna(macro_resistance) and ltp > (macro_resistance * 1.001)
    volume_confirms = vol_ratio_15m > BREAKOUT_VOLUME_RATIO_MIN
    is_breakout = bool(price_above_resistance and volume_confirms)

    if is_breakout:
        # Tighten the stop: swap the wide daily SuperTrend for the 15m one so
        # R:R stays tradable instead of being blown out by the daily-scale stop.
        stop_loss = intraday_stop if pd.notna(intraday_stop) else daily_stop
        decision = "BUY (INTRADAY TRIGGER)"
        breakout_status = "YES"
    else:
        stop_loss = daily_stop
        decision = "Watch only"
        breakout_status = "NO"

    return {
        'Decision': decision,
        'Breakout_Status': breakout_status,
        'Stop_Loss': round(stop_loss, 2) if pd.notna(stop_loss) else np.nan,
        'Vol_Ratio_15m': round(vol_ratio_15m, 2),
        'LTP': round(ltp, 2),
        # Extra context for logging/debugging — safe to ignore if you only need the five fields above.
        'Macro_Resistance': round(float(macro_resistance), 2) if pd.notna(macro_resistance) else np.nan,
        'Daily_RSI': round(rsi_val, 1) if pd.notna(rsi_val) else np.nan,
        'Daily_ADX': round(adx_val, 1) if pd.notna(adx_val) else np.nan,
        'Daily_SuperTrend_Stop': round(daily_stop, 2) if pd.notna(daily_stop) else np.nan,
        'Intraday_SuperTrend_Stop': round(intraday_stop, 2) if pd.notna(intraday_stop) else np.nan,
    }

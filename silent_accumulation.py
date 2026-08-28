"""
Silent Accumulation ("stealth trend") analytics.

The momentum path of this engine is tuned for chaotic retail energy: volume
shocks, wide daily ranges and 250-day breakouts. That logic structurally
ignores the opposite footprint - a stock drifting up 0.8-1.5% a day for weeks
on steady volume because an institution is filling a large order with VWAP
slices while there is no supply left to sell into it.

This module measures that footprint mathematically. Every function here is a
pure pandas/numpy transform on a daily OHLCV frame, so it can be reused by the
scanner, the backtester and any offline research script without touching the
broker.

Measured properties:
  1. Trend quality   - OLS slope of log(Close) and its R^2 (smoothness).
  2. Persistence     - how many of the last N sessions closed green.
  3. Volatility      - ATR% now vs. its own one-year distribution (stability).
  4. Spike absence   - the largest single-day move inside the trend window.
  5. EMA ride        - how tightly price hugs the rising 20 EMA.
  6. Volume steadiness - sustained participation without single-day blowoffs.
  7. Base tightness  - the pre-move consolidation that shook the weak hands out.
"""

import logging
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ('Open', 'High', 'Low', 'Close', 'Volume')


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Sorts by timestamp when present and drops rows with unusable OHLCV data."""
    if df is None or df.empty:
        return pd.DataFrame()
    frame = df.copy()
    if 'Timestamp' in frame.columns:
        frame['Timestamp'] = pd.to_datetime(frame['Timestamp'], errors='coerce')
        frame = frame.dropna(subset=['Timestamp']).sort_values('Timestamp')
    missing = [col for col in REQUIRED_COLUMNS if col not in frame.columns]
    if missing:
        return pd.DataFrame()
    frame = frame.dropna(subset=list(REQUIRED_COLUMNS))
    return frame


def _scale(value: float, floor: float, ceiling: float) -> float:
    """Maps `value` onto 0..1 between floor and ceiling (handles inverted ranges)."""
    if pd.isna(value) or ceiling == floor:
        return 0.0
    ratio = (value - floor) / (ceiling - floor)
    return float(max(0.0, min(1.0, ratio)))


def log_regression_slope(close: pd.Series, window: int) -> Dict[str, float]:
    """
    Fits an ordinary least squares line to log(Close) over `window` sessions.

    Working in log space makes the slope a compounding daily return, so a
    ₹100 stock and a ₹4,000 stock are directly comparable. R^2 is the
    "smoothness" term: a stock that grinds up every day fits the line almost
    perfectly, while one that gaps 20% and then chops sideways does not.
    """
    series = close.tail(window).astype(float)
    if len(series) < window or (series <= 0).any():
        return {'slope_pct': np.nan, 'r_squared': np.nan}

    y = np.log(series.to_numpy())
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)

    fitted = (slope * x) + intercept
    residual_ss = float(np.sum((y - fitted) ** 2))
    total_ss = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1.0 - (residual_ss / total_ss) if total_ss > 1e-12 else 0.0

    return {
        'slope_pct': float((np.exp(slope) - 1.0) * 100.0),
        'r_squared': float(max(0.0, min(1.0, r_squared))),
    }


def true_range(df: pd.DataFrame) -> pd.Series:
    """Wilder true range: the widest of today's range and the two gap ranges."""
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)


def atr_stability(df: pd.DataFrame, period: int, lookback: int) -> Dict[str, float]:
    """
    Expresses ATR as a percentage of price and ranks it against its own history.

    A silent mover is not merely low-volatility in absolute terms - it is quiet
    *relative to how it normally trades*. A percentile near 0 means the stock is
    advancing in its calmest tape of the past year, which is the signature of
    absorption rather than speculation.
    """
    atr = true_range(df).ewm(span=period, adjust=False).mean()
    close = df['Close'].astype(float)
    atr_pct_series = (atr / close.replace(0, np.nan)) * 100.0

    current = atr_pct_series.iloc[-1]
    history = atr_pct_series.tail(lookback).dropna()
    if pd.isna(current) or len(history) < period * 2:
        return {'atr_pct': float(current) if not pd.isna(current) else np.nan,
                'atr_percentile': np.nan}

    percentile = float((history < current).mean() * 100.0)
    return {'atr_pct': float(current), 'atr_percentile': percentile}


def trend_persistence(close: pd.Series, window: int) -> Dict[str, float]:
    """Counts green closes and the worst single-day drawdown inside the window."""
    changes = close.astype(float).pct_change().tail(window).dropna()
    if changes.empty:
        return {'green_days': 0, 'total_days': 0, 'green_ratio': 0.0, 'worst_day_pct': 0.0}
    green_days = int((changes > 0).sum())
    return {
        'green_days': green_days,
        'total_days': int(len(changes)),
        'green_ratio': float(green_days / len(changes)),
        'worst_day_pct': float(changes.min() * 100.0),
    }


def move_smoothness(close: pd.Series, window: int) -> Dict[str, float]:
    """Largest single-day advance in the window - the flash-move detector."""
    changes = close.astype(float).pct_change().tail(window).dropna()
    if changes.empty:
        return {'max_day_gain_pct': 0.0, 'avg_abs_move_pct': 0.0}
    return {
        'max_day_gain_pct': float(changes.max() * 100.0),
        'avg_abs_move_pct': float(changes.abs().mean() * 100.0),
    }


def ema_ride(df: pd.DataFrame, span: int, window: int, slope_pct: float = np.nan) -> Dict[str, float]:
    """
    Measures how tightly price rides a rising EMA.

    Structural moves track the 10/20 EMA; flash moves detach from it. Raw
    distance alone cannot judge that, because an EMA lags: a stock compounding
    a clean 1% a day *should* sit ~9% above its 20 EMA purely from the lag, and
    penalising it for that would reject exactly the setups being hunted. So the
    distance is scored against the lag the measured slope implies -
    `extension_ratio` near 1.0 means the stock is riding the average, while 2.5
    means it has detached from it.
    """
    close = df['Close'].astype(float)
    ema = close.ewm(span=span, adjust=False).mean()
    tail_close = close.tail(window)
    tail_ema = ema.tail(window)
    if tail_ema.empty or len(tail_close) < window:
        return {'days_above_ema': 0, 'above_ema_ratio': 0.0, 'avg_ema_distance_pct': np.nan,
                'ema_slope_pct': np.nan, 'current_ema_distance_pct': np.nan,
                'extension_ratio': np.nan}

    distance = ((tail_close - tail_ema) / tail_ema.replace(0, np.nan)) * 100.0
    ema_start = tail_ema.iloc[0]
    ema_slope = ((tail_ema.iloc[-1] / ema_start) - 1.0) * 100.0 / window if ema_start > 0 else np.nan

    avg_distance = float(distance.abs().mean())
    # Mean lag of an EMA of `span` periods is about (span - 1) / 2 sessions, so
    # a trend drifting `slope_pct` a day carries that much built-in separation.
    daily_drift = slope_pct if not pd.isna(slope_pct) else ema_slope
    expected_lag = max(daily_drift, 0.0) * ((span - 1) / 2.0) if not pd.isna(daily_drift) else np.nan
    if pd.isna(expected_lag):
        extension_ratio = np.nan
    else:
        extension_ratio = avg_distance / max(expected_lag, 1.0)

    return {
        'days_above_ema': int((tail_close > tail_ema).sum()),
        'above_ema_ratio': float((tail_close > tail_ema).mean()),
        'avg_ema_distance_pct': avg_distance,
        'ema_slope_pct': float(ema_slope) if not pd.isna(ema_slope) else np.nan,
        'current_ema_distance_pct': float(distance.iloc[-1]) if not pd.isna(distance.iloc[-1]) else np.nan,
        'extension_ratio': float(extension_ratio) if not pd.isna(extension_ratio) else np.nan,
    }


def volume_steadiness(volume: pd.Series, window: int, baseline: int) -> Dict[str, float]:
    """
    Separates sustained institutional participation from single-day blowoffs.

    `participation` is recent average volume over the pre-move baseline: an
    iceberg order lifts it modestly and keeps it there. `spike_ratio` is the
    biggest day in the window over the window's median: a low value means the
    buying is spread out rather than dumped in one euphoric bar.
    """
    vol = volume.astype(float)
    recent = vol.tail(window)
    prior = vol.iloc[:-window].tail(baseline) if len(vol) > window else pd.Series(dtype=float)

    recent_avg = float(recent.mean()) if not recent.empty else 0.0
    baseline_avg = float(prior.mean()) if not prior.empty else 0.0
    recent_median = float(recent.median()) if not recent.empty else 0.0

    return {
        'participation': float(recent_avg / baseline_avg) if baseline_avg > 0 else np.nan,
        'spike_ratio': float(recent.max() / recent_median) if recent_median > 0 else np.nan,
        'avg_volume': recent_avg,
    }


def base_tightness(close: pd.Series, window: int, base_window: int, lookback: int) -> Dict[str, float]:
    """
    Measures the consolidation that preceded the current advance.

    The supply vacuum is built during a boring sideways base, where impatient
    holders give up. Anchoring the base to a fixed offset does not work, because
    a stealth trend that is already three months old would drag its own advance
    into the "base" and look wide. Instead this scans the history before the
    current trend window and reports the *tightest* `base_window` stretch it
    finds: evidence that a genuine range-bound shakeout happened at some point
    before the drift began.
    """
    prior = close.astype(float).iloc[:-window].tail(lookback)
    if len(prior) < base_window:
        return {'base_range_pct': np.nan, 'base_days': int(len(prior))}

    rolling_high = prior.rolling(base_window).max()
    rolling_low = prior.rolling(base_window).min()
    ranges = ((rolling_high - rolling_low) / rolling_low.replace(0, np.nan)) * 100.0
    ranges = ranges.dropna()
    if ranges.empty:
        return {'base_range_pct': np.nan, 'base_days': int(len(prior))}
    return {'base_range_pct': float(ranges.min()), 'base_days': int(base_window)}


def _build_rationale(components: Dict[str, float], raw: Dict[str, Any]) -> str:
    """Human-readable explanation of what earned the score, strongest factor first."""
    phrases = []
    ranked = sorted(components.items(), key=lambda item: item[1], reverse=True)
    templates = {
        'trend': lambda: f"a {raw['Silent_Slope_Pct']:.2f}%/day regression slope fitting at R² {raw['Silent_R2']:.2f}",
        'persistence': lambda: f"{raw['Silent_Green_Days']}/{raw['Silent_Trend_Days']} green closes",
        'volatility': lambda: f"ATR at {raw['Silent_ATR_Pct']:.2f}% of price ({raw['Silent_ATR_Percentile']:.0f}th pctl of its own year)",
        'spike_absence': lambda: f"no day larger than {raw['Silent_Max_Day_Gain_Pct']:.1f}%",
        'ema_ride': lambda: f"price riding the {config.Silent.EMA_SPAN} EMA at {raw['Silent_EMA_Extension_Ratio']:.1f}x its natural lag",
        'volume': lambda: f"steady {raw['Silent_Volume_Participation']:.2f}x participation without volume blowoffs",
        'base': lambda: f"a {raw['Silent_Base_Range_Pct']:.1f}% pre-move base",
    }
    for name, score in ranked:
        if score <= 0 or name not in templates:
            continue
        try:
            phrase = templates[name]()
        except (KeyError, TypeError, ValueError):
            continue
        if 'nan' not in phrase:
            phrases.append(phrase)
        if len(phrases) == 3:
            break

    if not phrases:
        return "No silent accumulation footprint detected."
    return "Silent accumulation: " + ", ".join(phrases) + "."


def compute_silent_metrics(df: pd.DataFrame, cfg=None) -> Optional[Dict[str, Any]]:
    """
    Scores a daily frame on the silent-accumulation footprint.

    Returns a flat dict of `Silent_*` fields (safe to merge into a signal row),
    or None when there is not enough history to judge. The composite
    `Silent_Score` is 0-100 with the weights defined in config.Silent.
    """
    cfg = cfg or config.Silent
    frame = _clean_frame(df)
    if len(frame) < cfg.MIN_HISTORY_DAYS:
        return None

    close = frame['Close'].astype(float)
    if (close <= 0).any():
        return None

    trend = log_regression_slope(close, cfg.TREND_WINDOW)
    if pd.isna(trend['slope_pct']):
        return None

    volatility = atr_stability(frame, cfg.ATR_PERIOD, cfg.ATR_PERCENTILE_LOOKBACK)
    persistence = trend_persistence(close, cfg.PERSISTENCE_WINDOW)
    smoothness = move_smoothness(close, cfg.TREND_WINDOW)
    ride = ema_ride(frame, cfg.EMA_SPAN, cfg.TREND_WINDOW, slope_pct=trend['slope_pct'])
    volume = volume_steadiness(frame['Volume'], cfg.TREND_WINDOW, cfg.VOLUME_BASELINE_WINDOW)
    base = base_tightness(close, cfg.TREND_WINDOW, cfg.BASE_WINDOW, cfg.BASE_LOOKBACK)

    # --- Sub-scores, each normalised to 0..1 before weighting ---------------
    slope_fit = _scale(trend['slope_pct'], 0.0, cfg.IDEAL_SLOPE_PCT)
    # A parabolic slope is a flash move, not accumulation: taper past the ideal.
    if trend['slope_pct'] > cfg.MAX_SLOPE_PCT:
        slope_fit *= 0.5
    trend_unit = slope_fit * _scale(trend['r_squared'], cfg.MIN_R_SQUARED, cfg.IDEAL_R_SQUARED)

    persistence_unit = _scale(persistence['green_ratio'], cfg.MIN_GREEN_RATIO, cfg.IDEAL_GREEN_RATIO)

    # Prefer the self-relative percentile; fall back to absolute ATR% when the
    # frame is too short for a meaningful one-year distribution.
    if not pd.isna(volatility['atr_percentile']):
        volatility_unit = _scale(volatility['atr_percentile'], cfg.MAX_ATR_PERCENTILE, 0.0)
    else:
        volatility_unit = _scale(volatility['atr_pct'], cfg.MAX_ATR_PCT, 0.0)

    spike_unit = _scale(smoothness['max_day_gain_pct'], cfg.MAX_SINGLE_DAY_GAIN_PCT, 0.0)

    ema_unit = _scale(ride['above_ema_ratio'], cfg.MIN_ABOVE_EMA_RATIO, 1.0)
    if not pd.isna(ride['extension_ratio']):
        ema_unit *= _scale(ride['extension_ratio'], cfg.MAX_EMA_EXTENSION_RATIO, cfg.IDEAL_EMA_EXTENSION_RATIO)
    if pd.isna(ride['ema_slope_pct']) or ride['ema_slope_pct'] <= 0:
        ema_unit = 0.0

    if pd.isna(volume['participation']):
        volume_unit = 0.0
    else:
        # Reward sustained-but-unremarkable participation, punish blowoff bars.
        volume_unit = _scale(volume['participation'], cfg.MIN_VOLUME_PARTICIPATION, cfg.IDEAL_VOLUME_PARTICIPATION)
        if not pd.isna(volume['spike_ratio']):
            volume_unit *= _scale(volume['spike_ratio'], cfg.MAX_VOLUME_SPIKE_RATIO, 1.0)

    base_unit = 0.0 if pd.isna(base['base_range_pct']) else _scale(base['base_range_pct'], cfg.MAX_BASE_RANGE_PCT, 0.0)

    components = {
        'trend': trend_unit * cfg.WEIGHTS['trend'],
        'persistence': persistence_unit * cfg.WEIGHTS['persistence'],
        'volatility': volatility_unit * cfg.WEIGHTS['volatility'],
        'spike_absence': spike_unit * cfg.WEIGHTS['spike_absence'],
        'ema_ride': ema_unit * cfg.WEIGHTS['ema_ride'],
        'volume': volume_unit * cfg.WEIGHTS['volume'],
        'base': base_unit * cfg.WEIGHTS['base'],
    }
    score = round(float(sum(components.values())), 1)

    if score >= cfg.SCORE_STRONG:
        grade = 'STEALTH LEADER 🕵️'
    elif score >= cfg.SCORE_QUALIFIED:
        grade = 'ACCUMULATING 📈'
    elif score >= cfg.SCORE_WATCH:
        grade = 'FORMING 👀'
    else:
        grade = 'NOISY ⚠️'

    metrics = {
        'Silent_Score': score,
        'Silent_Grade': grade,
        'Silent_Slope_Pct': round(trend['slope_pct'], 3),
        'Silent_R2': round(trend['r_squared'], 3),
        'Silent_Green_Days': persistence['green_days'],
        'Silent_Trend_Days': persistence['total_days'],
        'Silent_ATR_Pct': round(volatility['atr_pct'], 2) if not pd.isna(volatility['atr_pct']) else np.nan,
        'Silent_ATR_Percentile': round(volatility['atr_percentile'], 1) if not pd.isna(volatility['atr_percentile']) else np.nan,
        'Silent_Max_Day_Gain_Pct': round(smoothness['max_day_gain_pct'], 2),
        'Silent_Worst_Day_Pct': round(persistence['worst_day_pct'], 2),
        'Silent_Above_EMA_Days': ride['days_above_ema'],
        'Silent_EMA_Distance_Pct': round(ride['avg_ema_distance_pct'], 2) if not pd.isna(ride['avg_ema_distance_pct']) else np.nan,
        'Silent_EMA_Extension_Ratio': round(ride['extension_ratio'], 2) if not pd.isna(ride['extension_ratio']) else np.nan,
        'Silent_EMA_Slope_Pct': round(ride['ema_slope_pct'], 3) if not pd.isna(ride['ema_slope_pct']) else np.nan,
        'Silent_Volume_Participation': round(volume['participation'], 2) if not pd.isna(volume['participation']) else np.nan,
        'Silent_Volume_Spike_Ratio': round(volume['spike_ratio'], 2) if not pd.isna(volume['spike_ratio']) else np.nan,
        'Silent_Base_Range_Pct': round(base['base_range_pct'], 2) if not pd.isna(base['base_range_pct']) else np.nan,
        'Silent_Components': {k: round(v, 2) for k, v in components.items()},
    }
    metrics['Silent_Qualified'] = bool(
        score >= cfg.SCORE_QUALIFIED
        and trend['slope_pct'] >= cfg.MIN_SLOPE_PCT
        and trend['r_squared'] >= cfg.MIN_R_SQUARED
        and persistence['green_ratio'] >= cfg.MIN_GREEN_RATIO
        and smoothness['max_day_gain_pct'] <= cfg.MAX_SINGLE_DAY_GAIN_PCT
    )
    metrics['Silent_Rationale'] = _build_rationale(components, metrics)
    return metrics


def is_silent_mover(df: pd.DataFrame, cfg=None) -> bool:
    """Convenience predicate for screeners and notebooks."""
    metrics = compute_silent_metrics(df, cfg=cfg)
    return bool(metrics and metrics['Silent_Qualified'])

import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from mtf_breakout import analyze_mtf_breakout, get_target_candle_index
from utils import install_and_import

pytest = install_and_import('pytest')


def _make_daily_df(today_close: float, today_high: float, rows: int = 30) -> pd.DataFrame:
    """Linear uptrend daily OHLCV, with the *last* row overridden to `today_close`/`today_high`
    so tests can control exactly where "today" sits relative to the prior-20-day resistance."""
    closes = [100.0 + i for i in range(rows - 1)] + [today_close]
    highs = [c + 0.5 for c in closes[:-1]] + [today_high]
    lows = [c - 0.5 for c in closes[:-1]] + [today_close - 0.5]
    opens = [c - 0.3 for c in closes[:-1]] + [today_close - 0.3]
    return pd.DataFrame({
        'Timestamp': pd.date_range(start='2024-01-01', periods=rows, freq='D'),
        'Open': opens,
        'High': highs,
        'Low': lows,
        'Close': closes,
        'Volume': [10000] * rows,
    })


def _resistance_of(daily_df: pd.DataFrame) -> float:
    """Mirrors mtf_breakout's own macro-resistance formula so buffer tests aren't hand-computed."""
    return float(daily_df['High'].shift(1).rolling(window=20).max().iloc[-1])


def _make_intraday_df(rows: int = 40, last_volume: float = 10000.0, last_close: float = 100.0) -> pd.DataFrame:
    timestamps = pd.date_range(start='2024-01-02 09:15', periods=rows, freq='15min')
    volumes = [1000.0] * (rows - 1) + [last_volume]
    closes = [100.0] * (rows - 1) + [last_close]
    return pd.DataFrame({
        'Timestamp': timestamps,
        'Open': closes,
        'High': [c + 0.5 for c in closes],
        'Low': [c - 0.5 for c in closes],
        'Close': closes,
        'Volume': volumes,
    })


# --- get_target_candle_index -------------------------------------------------

def test_fresh_boundary_steps_back_to_last_closed_candle():
    now = datetime(2024, 1, 1, 10, 15, 30)  # 30s into a new 15m bucket
    df = _make_intraday_df(rows=5)
    assert get_target_candle_index(df, now=now) == -2


def test_fresh_boundary_with_single_row_falls_back_safely():
    now = datetime(2024, 1, 1, 10, 15, 30)
    df = _make_intraday_df(rows=1)
    assert get_target_candle_index(df, now=now) == -1


def test_mid_bucket_reads_current_forming_candle():
    now = datetime(2024, 1, 1, 10, 20, 0)  # 5 minutes into the bucket
    df = _make_intraday_df(rows=5)
    assert get_target_candle_index(df, now=now) == -1


# --- analyze_mtf_breakout: data-sufficiency guards ----------------------------

def test_empty_inputs_return_no_data():
    result = analyze_mtf_breakout(pd.DataFrame(), pd.DataFrame())
    assert result['Decision'] == 'NO DATA'
    assert result['Breakout_Status'] == 'NO'


def test_insufficient_history_returns_no_data():
    daily = _make_daily_df(today_close=110, today_high=110.5, rows=10)  # < 21 rows required
    intraday = _make_intraday_df(rows=40)
    result = analyze_mtf_breakout(daily, intraday)
    assert result['Decision'] == 'NO DATA'


# --- analyze_mtf_breakout: breakout gating ------------------------------------

def test_price_and_volume_confirm_breakout():
    daily = _make_daily_df(today_close=140, today_high=140.5)
    resistance = _resistance_of(daily)
    intraday = _make_intraday_df(last_volume=10000.0, last_close=resistance + 10)  # 10x volume, well above resistance
    now = datetime(2024, 1, 2, 10, 5, 0)  # comfortably mid-bucket, no proration edge case
    result = analyze_mtf_breakout(daily, intraday, now=now)
    assert result['Breakout_Status'] == 'YES'
    assert result['Decision'] == 'BUY (INTRADAY TRIGGER)'


def test_price_below_resistance_is_not_a_breakout_even_with_volume():
    daily = _make_daily_df(today_close=120, today_high=120.5)  # below the ~128.5 resistance
    intraday = _make_intraday_df(last_volume=10000.0, last_close=125.0)
    now = datetime(2024, 1, 2, 10, 5, 0)
    result = analyze_mtf_breakout(daily, intraday, now=now)
    assert result['Breakout_Status'] == 'NO'
    assert result['Decision'] == 'Watch only'


def test_price_above_resistance_without_volume_confirmation_is_not_a_breakout():
    daily = _make_daily_df(today_close=140, today_high=140.5)
    resistance = _resistance_of(daily)
    intraday = _make_intraday_df(last_volume=1000.0, last_close=resistance + 10)  # same as baseline volume, no spike
    # Read near the end of the 15m bucket (890s elapsed) so the proration factor is ~1.0 -
    # isolates "no volume spike" from the proration behavior covered by the tests below.
    now = datetime(2024, 1, 2, 10, 14, 50)
    result = analyze_mtf_breakout(daily, intraday, now=now)
    assert result['Breakout_Status'] == 'NO'


def test_penetration_buffer_rejects_marginal_overshoot():
    daily = _make_daily_df(today_close=140, today_high=140.5)
    resistance = _resistance_of(daily)
    intraday = _make_intraday_df(last_volume=10000.0, last_close=resistance * 1.0005)  # +0.05%, under the 0.1% buffer
    now = datetime(2024, 1, 2, 10, 5, 0)
    result = analyze_mtf_breakout(daily, intraday, now=now)
    assert result['Breakout_Status'] == 'NO'


def test_penetration_buffer_accepts_clear_overshoot():
    daily = _make_daily_df(today_close=140, today_high=140.5)
    resistance = _resistance_of(daily)
    intraday = _make_intraday_df(last_volume=10000.0, last_close=resistance * 1.002)  # +0.2%, clears the buffer
    now = datetime(2024, 1, 2, 10, 5, 0)
    result = analyze_mtf_breakout(daily, intraday, now=now)
    assert result['Breakout_Status'] == 'YES'


# --- analyze_mtf_breakout: time-normalized volume ratio -----------------------

def test_volume_ratio_is_prorated_early_in_a_forming_candle():
    """A candle only just past the Fractional Candle Guard's 60s window should show a
    much higher ratio than the same absolute volume read near the end of its 15m bucket,
    because the baseline is scaled down to match how little of the bar has actually printed."""
    daily = _make_daily_df(today_close=120, today_high=120.5)  # breakout status irrelevant here
    intraday = _make_intraday_df(rows=40, last_volume=2000.0, last_close=100.0)

    early = analyze_mtf_breakout(daily, intraday, now=datetime(2024, 1, 2, 10, 1, 5))   # 65s into the bucket
    late = analyze_mtf_breakout(daily, intraday, now=datetime(2024, 1, 2, 10, 14, 50))  # 890s into the bucket

    assert early['Vol_Ratio_15m'] > late['Vol_Ratio_15m']
    # Late in the bucket, the prorated baseline is ~= the full-bar baseline, so the ratio
    # should be close to the naive (unprorated) current/baseline volume ratio.
    assert late['Vol_Ratio_15m'] == pytest.approx(2000.0 / 1050.0, rel=0.05)


def test_fresh_boundary_candle_is_not_prorated():
    """When the guard steps back to target_idx=-2 (a fully-closed prior bar), that bar's
    volume is complete and must not be scaled down just because `now` is only 10s into the
    *new* bucket - only the still-forming current candle (target_idx=-1) should be prorated.

    Row -2 has the default 1000 volume (only the last row was overridden to 2000), and the
    rolling(20) baseline ending at -2 excludes that last row entirely, so an *unprorated*
    read should land at ~1000/1000 = 1.0. If proration leaked into the target_idx=-2 path,
    dividing the baseline by (10s / 900s) would inflate this to ~90x instead.
    """
    daily = _make_daily_df(today_close=120, today_high=120.5)
    intraday = _make_intraday_df(rows=40, last_volume=2000.0, last_close=100.0)

    fresh_boundary = analyze_mtf_breakout(daily, intraday, now=datetime(2024, 1, 2, 10, 15, 10))  # 10s in, guard fires

    assert fresh_boundary['Vol_Ratio_15m'] == pytest.approx(1.0, rel=0.05)

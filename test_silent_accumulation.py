"""Tests for the silent-accumulation footprint detector."""

import numpy as np
import pandas as pd

import config
from scanner_engine import HybridScanner
from silent_accumulation import (
    atr_stability,
    compute_silent_metrics,
    is_silent_mover,
    log_regression_slope,
    trend_persistence,
)


def _frame(closes, volumes=None, min_range=0.004):
    """
    Builds a daily OHLCV frame around a close series.

    The intraday range is tied to that day's realized move, so a choppy stretch
    produces a genuinely higher ATR than a quiet drift - otherwise the ATR
    stability checks would be measuring the fixture, not the price action.
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    if volumes is None:
        volumes = np.full(n, 500_000.0)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    moves = np.abs(np.diff(closes, prepend=closes[0]) / closes)
    spread = np.maximum(min_range, moves * 0.8)
    highs = np.maximum(opens, closes) * (1 + spread)
    lows = np.minimum(opens, closes) * (1 - spread)
    return pd.DataFrame({
        'Timestamp': pd.date_range(start='2024-01-01', periods=n, freq='B'),
        'Open': opens,
        'High': highs,
        'Low': lows,
        'Close': closes,
        'Volume': volumes,
    })


def _range_bound_base(days, seed, noise=0.014, anchor=100.0, pull=0.05):
    """A mean-reverting, boring consolidation - the shakeout phase (Stage 1)."""
    rng = np.random.default_rng(seed)
    price = anchor
    path = []
    for _ in range(days):
        price *= 1 + rng.normal(0, noise) + pull * ((anchor / price) - 1.0)
        path.append(price)
    return np.asarray(path)


def _silent_closes(base_days=220, trend_days=45, drift=0.009, seed=7):
    """A long, tight base followed by a smooth ~0.9%/day institutional drift."""
    rng = np.random.default_rng(seed)
    base = _range_bound_base(base_days, seed)
    steps = drift + rng.normal(0, 0.0015, trend_days)
    trend = base[-1] * (1 + steps).cumprod()
    return np.concatenate([base, trend])


def _spiky_closes(base_days=250, chop_days=14, seed=11):
    """A volatile stock that gaps ~20% on one day and then chops - a flash move."""
    rng = np.random.default_rng(seed)
    base = _range_bound_base(base_days, seed, noise=0.025)
    spike = base[-1] * 1.20
    chop = spike * (1 + rng.normal(0, 0.025, chop_days)).cumprod()
    return np.concatenate([base, [spike], chop])


def test_log_regression_slope_recovers_known_drift():
    """A pure 1%/day compounding series must return a 1.0% slope at R^2 = 1."""
    closes = 100 * (1.01 ** np.arange(40))
    result = log_regression_slope(pd.Series(closes), window=20)
    assert abs(result['slope_pct'] - 1.0) < 1e-6
    assert result['r_squared'] > 0.999


def test_regression_r_squared_punishes_a_single_gap():
    """Same net gain, but delivered in one jump, must fit the line far worse."""
    smooth = pd.Series(100 * (1.01 ** np.arange(20)))
    jumpy = pd.Series(np.concatenate([np.full(10, 100.0), np.full(10, 100 * 1.01 ** 19)]))
    assert log_regression_slope(smooth, 20)['r_squared'] > log_regression_slope(jumpy, 20)['r_squared']


def test_trend_persistence_counts_green_days():
    closes = [100, 101, 102, 101.5, 103, 104, 105, 104.8, 106, 107, 108]
    result = trend_persistence(pd.Series(closes), window=10)
    assert result['total_days'] == 10
    assert result['green_days'] == 8
    assert abs(result['green_ratio'] - 0.8) < 1e-9


def test_atr_stability_ranks_quiet_tape_low():
    """A stock that just calmed down should sit in the low end of its own range."""
    rng = np.random.default_rng(3)
    wild = _range_bound_base(200, seed=3, noise=0.035)
    calm = wild[-1] * (1 + rng.normal(0.001, 0.003, 60)).cumprod()
    df = _frame(np.concatenate([wild, calm]))
    result = atr_stability(df, period=14, lookback=250)
    assert result['atr_percentile'] < 30.0


def test_silent_stock_scores_above_qualified_band():
    metrics = compute_silent_metrics(_frame(_silent_closes()))
    assert metrics is not None
    assert metrics['Silent_Score'] >= config.Silent.SCORE_QUALIFIED, metrics
    assert metrics['Silent_Qualified'] is True
    assert metrics['Silent_Green_Days'] >= 7
    assert metrics['Silent_Max_Day_Gain_Pct'] < config.Silent.MAX_SINGLE_DAY_GAIN_PCT
    assert 'Silent accumulation' in metrics['Silent_Rationale']


def test_flash_mover_scores_below_silent_mover():
    silent = compute_silent_metrics(_frame(_silent_closes()))
    spiky = compute_silent_metrics(_frame(_spiky_closes()))
    assert spiky is not None
    assert spiky['Silent_Score'] < silent['Silent_Score']
    assert not spiky['Silent_Qualified']


def test_volume_blowoff_discounts_the_volume_component():
    """Identical price action, but one stock dumps its volume into a single bar."""
    closes = _silent_closes()
    steady_volume = np.full(len(closes), 500_000.0)
    blowoff_volume = steady_volume.copy()
    blowoff_volume[-5] = 12_000_000.0

    steady = compute_silent_metrics(_frame(closes, steady_volume))
    blowoff = compute_silent_metrics(_frame(closes, blowoff_volume))
    assert blowoff['Silent_Components']['volume'] < steady['Silent_Components']['volume']
    assert blowoff['Silent_Score'] < steady['Silent_Score']


def test_downtrend_is_rejected():
    closes = 200 * (0.995 ** np.arange(300))
    metrics = compute_silent_metrics(_frame(closes))
    assert metrics is not None
    assert not metrics['Silent_Qualified']
    assert metrics['Silent_Components']['trend'] == 0.0


def test_short_history_returns_none():
    assert compute_silent_metrics(_frame(100 * (1.01 ** np.arange(30)))) is None
    assert compute_silent_metrics(pd.DataFrame()) is None


def test_is_silent_mover_predicate():
    assert is_silent_mover(_frame(_silent_closes()))
    assert not is_silent_mover(_frame(_spiky_closes()))


def test_daily_metrics_carry_silent_fields():
    scanner = HybridScanner()
    metrics = scanner.compute_daily_metrics(_frame(_silent_closes()))
    assert metrics is not None
    assert 'Silent_Score' in metrics and 'Silent_Rationale' in metrics


def test_scan_silent_builds_a_tradeable_signal():
    scanner = HybridScanner()
    closes = _silent_closes()
    # Traded value must clear the ₹10 crore liquidity floor used by the scanner.
    df = _frame(closes, volumes=np.full(len(closes), 1_000_000.0))
    signal = scanner.scan_silent('TESTSTK', df, rs_percentile=70.0, sector_rs=60.0)

    assert signal is not None
    assert signal['Horizon'] == 'SILENT'
    assert signal['Stop'] < signal['Trigger'] < signal['Target']
    assert signal['Risk_Reward'] == config.Silent.RR_RATIO
    assert signal['Qty'] > 0
    assert signal['Execution_Recommendation'] in ('BUY TODAY', 'WATCH')
    assert signal['Score'] == signal['Silent_Score']


def test_scan_dispatches_silent_without_intraday_data():
    """SILENT must not require the 15-minute frame the momentum path needs."""
    scanner = HybridScanner()
    closes = _silent_closes()
    df = _frame(closes, volumes=np.full(len(closes), 1_000_000.0))
    signal = scanner.scan('TESTSTK', df, pd.DataFrame(), rs_percentile=70.0,
                          sector_rs=60.0, strategy='SILENT')
    assert signal is not None and signal['Horizon'] == 'SILENT'


def test_illiquid_silent_stock_is_filtered_out():
    scanner = HybridScanner()
    closes = _silent_closes()
    df = _frame(closes, volumes=np.full(len(closes), 500.0))
    assert scanner.scan_silent('TESTSTK', df, rs_percentile=70.0, sector_rs=60.0) is None

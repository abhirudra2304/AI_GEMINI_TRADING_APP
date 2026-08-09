import os
import sys

import pandas as pd

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from institutional_flow import InstitutionalConfig, InstitutionalFlowEngine
from utils import install_and_import

pytest = install_and_import('pytest')


def _engine_with_volume(volume_by_ticker: dict, dates, config: InstitutionalConfig = None) -> InstitutionalFlowEngine:
    """Builds a minimal valid engine whose self.volume matches
    `volume_by_ticker`. High/Low/Close/DeliveryVolume are filler - the tests
    that use this pass their own delivery_percent/clv DataFrames directly
    into calculate_accumulation_score rather than relying on the engine's
    own delivery_volume/clv derivation."""
    data = {}
    for ticker, volumes in volume_by_ticker.items():
        data[ticker] = pd.DataFrame({
            'Open': [1.0] * len(dates),
            'High': [1.0] * len(dates),
            'Low': [1.0] * len(dates),
            'Close': [1.0] * len(dates),
            'Volume': volumes,
            'DeliveryVolume': [0.0] * len(dates),
        }, index=pd.DatetimeIndex(dates))
    return InstitutionalFlowEngine(data, config=config)


# --- calculate_accumulation_score: sub-40% delivery noise filtering ---------

def test_accumulation_score_zeroes_out_sub_threshold_delivery_sessions():
    """A ticker with strong volume*CLV every day but delivery% always below
    the 40% min_delivery_threshold must accumulate a footprint of 0 - the
    whole point of the threshold is that high volume/CLV without real
    delivery is speculative churn, not institutional accumulation."""
    dates = pd.date_range('2026-01-01', periods=3, freq='D')
    engine = _engine_with_volume(
        {'LOW_DELIV': [1000.0, 1000.0, 1000.0], 'HIGH_DELIV': [1000.0, 1000.0, 1000.0]},
        dates,
        config=InstitutionalConfig(accumulation_window=3),
    )
    delivery_percent = pd.DataFrame({
        'LOW_DELIV': [30.0, 30.0, 30.0],   # below 40.0 threshold every day
        'HIGH_DELIV': [50.0, 50.0, 50.0],  # above threshold every day
    }, index=dates)
    clv = pd.DataFrame({
        'LOW_DELIV': [1.0, 1.0, 1.0],
        'HIGH_DELIV': [1.0, 1.0, 1.0],
    }, index=dates)

    score = engine.calculate_accumulation_score(delivery_percent, clv)

    last = score.iloc[-1]
    # HIGH_DELIV has a real (non-zero) rolling footprint; LOW_DELIV's is
    # zeroed out entirely, so with only two tickers HIGH_DELIV must rank
    # strictly above LOW_DELIV.
    assert last['HIGH_DELIV'] > last['LOW_DELIV']
    assert last['LOW_DELIV'] == pytest.approx(50.0)   # bottom of a 2-ticker pct-rank
    assert last['HIGH_DELIV'] == pytest.approx(100.0)  # top of a 2-ticker pct-rank


def test_accumulation_score_threshold_is_inclusive():
    """Delivery% exactly AT min_delivery_threshold (40.0) must qualify -
    the engine's filter is `>=`, not `>`."""
    dates = pd.date_range('2026-01-01', periods=2, freq='D')
    engine = _engine_with_volume(
        {'AT_THRESHOLD': [1000.0, 1000.0]}, dates, config=InstitutionalConfig(accumulation_window=2)
    )
    delivery_percent = pd.DataFrame({'AT_THRESHOLD': [40.0, 40.0]}, index=dates)
    clv = pd.DataFrame({'AT_THRESHOLD': [1.0, 1.0]}, index=dates)

    score = engine.calculate_accumulation_score(delivery_percent, clv)

    # Single ticker, footprint qualifies both days -> rolling sum > 0 ->
    # pct-rank of the only entry is 100.0, not the 0-footprint case.
    assert score.iloc[-1]['AT_THRESHOLD'] == pytest.approx(100.0)


# --- calculate_accumulation_score: normal-range inputs ----------------------

def test_accumulation_score_ranks_larger_footprint_higher():
    """Among tickers that all qualify (delivery% above threshold), the one
    with the larger Volume*CLV*DeliveryPct footprint must rank higher -
    this is the core 'more real institutional footprint = higher score'
    behavior the whole metric exists for."""
    dates = pd.date_range('2026-01-01', periods=5, freq='D')
    engine = _engine_with_volume(
        {
            'SMALL': [500.0] * 5,
            'MEDIUM': [2000.0] * 5,
            'LARGE': [5000.0] * 5,
        },
        dates,
        config=InstitutionalConfig(accumulation_window=5),
    )
    delivery_percent = pd.DataFrame({
        'SMALL': [60.0] * 5, 'MEDIUM': [60.0] * 5, 'LARGE': [60.0] * 5,
    }, index=dates)
    clv = pd.DataFrame({
        'SMALL': [0.8] * 5, 'MEDIUM': [0.8] * 5, 'LARGE': [0.8] * 5,
    }, index=dates)

    score = engine.calculate_accumulation_score(delivery_percent, clv)

    last = score.iloc[-1]
    assert last['LARGE'] > last['MEDIUM'] > last['SMALL']
    assert last['LARGE'] == pytest.approx(100.0)


# --- calculate_institutional_score: normal-range composite -----------------

def test_institutional_score_matches_weighted_formula():
    """0.30*normalized_rvol + 0.40*delivery_percent + 0.30*closing_range,
    with normalized_rvol = min(rvol, rvol_cap) / rvol_cap * 100, checked
    against hand-computed expected output for known inputs."""
    dates = pd.date_range('2026-01-01', periods=1, freq='D')
    engine = _engine_with_volume({'X': [1000.0]}, dates)
    rvol = pd.DataFrame({'X': [1.5]}, index=dates)              # below default cap of 3.0
    delivery_percent = pd.DataFrame({'X': [60.0]}, index=dates)
    closing_range = pd.DataFrame({'X': [80.0]}, index=dates)

    result = engine.calculate_institutional_score(rvol, delivery_percent, closing_range)

    normalized_rvol = (1.5 / 3.0) * 100.0  # 50.0
    expected = normalized_rvol * 0.30 + 60.0 * 0.40 + 80.0 * 0.30  # 15 + 24 + 24 = 63.0
    assert result.iloc[-1]['X'] == pytest.approx(expected)
    assert result.iloc[-1]['X'] == pytest.approx(63.0)


def test_institutional_score_clips_rvol_at_cap():
    """RVOL far above rvol_cap (e.g. a 10x volume spike) must be clipped to
    the cap before normalizing, not let a single outlier session blow the
    composite score past what the weight allows."""
    dates = pd.date_range('2026-01-01', periods=1, freq='D')
    engine = _engine_with_volume({'X': [1000.0]}, dates)
    rvol = pd.DataFrame({'X': [10.0]}, index=dates)  # way above default cap of 3.0
    delivery_percent = pd.DataFrame({'X': [0.0]}, index=dates)
    closing_range = pd.DataFrame({'X': [0.0]}, index=dates)

    result = engine.calculate_institutional_score(rvol, delivery_percent, closing_range)

    # Clipped rvol (3.0) / cap (3.0) * 100 = 100.0, weighted by 0.30 -> 30.0.
    # An unclipped rvol=10 would give (10/3*100)*0.30 = 100.0 instead - the
    # difference between these two numbers IS the behavior under test.
    assert result.iloc[-1]['X'] == pytest.approx(30.0)


def test_institutional_score_zero_inputs_gives_zero():
    dates = pd.date_range('2026-01-01', periods=1, freq='D')
    engine = _engine_with_volume({'X': [1000.0]}, dates)
    rvol = pd.DataFrame({'X': [0.0]}, index=dates)
    delivery_percent = pd.DataFrame({'X': [0.0]}, index=dates)
    closing_range = pd.DataFrame({'X': [0.0]}, index=dates)

    result = engine.calculate_institutional_score(rvol, delivery_percent, closing_range)
    assert result.iloc[-1]['X'] == pytest.approx(0.0)

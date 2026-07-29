"""
Golden-value regression tests for the scoring pipeline shared by live scanning,
historical signal generation (historical_generator.py), and backtesting
(backtest.py). All three consume the *same* HybridScanner.scan() /
_compute_btst_score() / utils.add_decision_scores() functions rather than
separate reimplementations - so there is only one scoring path to protect,
not two to reconcile.

These tests pin exact outputs for a fixed synthetic input. If a future change
to scan()/_compute_btst_score()/add_decision_scores() breaks this test, that
is the point: it forces an explicit, deliberate decision to update the pinned
values (and think about what already-persisted historical signals in
signals.db now mean) instead of letting the scoring formulas drift silently.
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from scanner_engine import HybridScanner
from earnings_manager import EarningsAnalyzer
from utils import add_decision_scores, install_and_import

pytest = install_and_import('pytest')

# No earnings risk, so the veto never fires and doesn't perturb the pinned
# scores below - and it keeps this test offline/deterministic (evaluate_risk
# otherwise hits yfinance over the network per symbol).
_NO_EARNINGS_RISK = {"Earnings_Date": "Unknown", "Days_To_Earnings": 999, "Earnings_Risk": "UNKNOWN"}


def _make_fixture():
    rows = 300
    closes = [100.0 + i for i in range(rows)]
    daily = pd.DataFrame({
        'Timestamp': pd.date_range(start='2023-01-01', periods=rows, freq='D'),
        'Open': [c - 0.3 for c in closes],
        'High': [c + 0.5 for c in closes],
        'Low': [c - 1.0 for c in closes],
        'Close': closes,
        'Volume': [500000] * rows,
    })
    df_15min = pd.DataFrame({
        'Timestamp': pd.date_range(start='2023-10-28 09:15', periods=5, freq='15min'),
        'Open': [398.0, 398.5, 399.0, 399.2, 399.4],
        'High': [398.6, 399.1, 399.3, 399.5, 399.8],
        'Low': [397.8, 398.3, 398.8, 399.0, 399.2],
        'Close': [398.5, 399.0, 399.2, 399.4, 399.6],
        'Volume': [50000, 55000, 60000, 90000, 120000],
    })
    return daily, df_15min


def _scan(strategy: str) -> dict:
    daily, df_15min = _make_fixture()
    scanner = HybridScanner()
    d_metrics = scanner.compute_daily_metrics(daily)
    with patch.object(scanner.earnings_engine, 'evaluate_risk', return_value=_NO_EARNINGS_RISK):
        return scanner.scan(
            'TESTSTOCK-EQ', daily, df_15min,
            rs_percentile=75.0, sector_rs=65.0, regime_mult=1.0,
            strategy=strategy, daily_metrics=d_metrics,
        )


def test_btst_scoring_is_pinned():
    signal = _scan('BTST')
    assert signal['Score'] == pytest.approx(78.6, abs=0.05)
    assert signal['Strength'] == 'STRONG 🔥'
    assert signal['BTST_Score'] == pytest.approx(74.42, abs=0.05)
    assert signal['Vol_Ratio'] == pytest.approx(1.71, abs=0.01)
    assert signal['Breakout250'] == 'YES'
    assert signal['Stop'] == pytest.approx(396.75, abs=0.01)
    assert signal['Target'] == pytest.approx(402.38, abs=0.01)
    assert signal['Risk_Reward'] == pytest.approx(1.5, abs=0.01)

    # Decision_Score/BTST_Final_Score pinned values updated 2026-07-29: Sector_RS
    # is no longer cooled/inverted in add_decision_scores (see utils.py) -
    # sector rotation persists rather than mean-reverts, so fading sector
    # leadership was the wrong call. sector_rs=65.0 in the fixture above shifts
    # Decision_Score by exactly +4.5 (= 65.0 * 0.15 - (100-65.0) * 0.15).
    ranked = add_decision_scores(pd.DataFrame([signal]))
    assert ranked.iloc[0]['Decision_Score'] == pytest.approx(27.2, abs=0.05)
    assert ranked.iloc[0]['BTST_Final_Score'] == pytest.approx(55.5, abs=0.05)


def test_swing_scoring_is_pinned():
    signal = _scan('SWING')
    assert signal['Score'] == pytest.approx(78.6, abs=0.05)
    assert signal['Strength'] == 'STRONG 🔥'
    assert signal['Vol_Ratio'] == pytest.approx(1.71, abs=0.01)
    assert signal['Breakout250'] == 'YES'
    assert signal['Stop'] == pytest.approx(396.0, abs=0.01)
    assert signal['Target'] == pytest.approx(405.0, abs=0.01)
    assert signal['Risk_Reward'] == pytest.approx(2.0, abs=0.01)

    # See test_btst_scoring_is_pinned above for why Decision_Score's pinned
    # value changed (Sector_RS is no longer cooled/inverted, 2026-07-29).
    ranked = add_decision_scores(pd.DataFrame([signal]))
    assert ranked.iloc[0]['Decision_Score'] == pytest.approx(27.2, abs=0.05)
    # SWING carries no BTST_Score, so BTST_Final_Score falls back to the
    # Execution_Score-based legacy formula in add_decision_scores.
    assert ranked.iloc[0]['BTST_Final_Score'] == pytest.approx(19.0, abs=0.05)


def test_earnings_veto_applies_regardless_of_strategy():
    """Regression guard for the BTST/SWING earnings-veto fix: HIGH RISK must
    downgrade Strength/Execution_Recommendation for every strategy, not just
    strategies that produce a 'BUY TODAY' Execution_Recommendation."""
    daily, df_15min = _make_fixture()
    scanner = HybridScanner()
    d_metrics = scanner.compute_daily_metrics(daily)
    high_risk = {"Earnings_Date": "2023-11-01", "Days_To_Earnings": 2, "Earnings_Risk": "HIGH RISK"}

    for strategy in ('BTST', 'SWING'):
        with patch.object(scanner.earnings_engine, 'evaluate_risk', return_value=high_risk):
            signal = scanner.scan(
                'TESTSTOCK-EQ', daily, df_15min,
                rs_percentile=75.0, sector_rs=65.0, regime_mult=1.0,
                strategy=strategy, daily_metrics=d_metrics,
            )
        assert signal['Execution_Recommendation'] == 'AVOID (EARNINGS)', strategy
        assert signal['Strength'] == 'EVENT RISK 🛑', strategy


def test_live_close_overrides_stale_daily_close():
    """Regression guard for the frozen-LTP bug: fast execution scans reuse
    Discovery's cached daily_metrics/df_daily (see orchestrator.py's scan_stock),
    so without an explicit live_close override, cp silently pins to whatever
    Close Discovery captured, even while df_15min (and Vol_Ratio) keep updating
    live every cycle - producing a stale-price/live-volume mismatch.

    live_close must win over daily_metrics['Close'] when provided, so LTP tracks
    the same live 15m data Vol_Ratio already does.
    """
    daily, df_15min = _make_fixture()
    scanner = HybridScanner()
    d_metrics = scanner.compute_daily_metrics(daily)
    stale_close = d_metrics['Close']
    live_price = stale_close + 15.0  # simulates real intraday movement since Discovery ran

    with patch.object(scanner.earnings_engine, 'evaluate_risk', return_value=_NO_EARNINGS_RISK):
        stale_signal = scanner.scan(
            'TESTSTOCK-EQ', daily, df_15min,
            rs_percentile=75.0, sector_rs=65.0, regime_mult=1.0,
            strategy='SWING', daily_metrics=d_metrics,
        )
        live_signal = scanner.scan(
            'TESTSTOCK-EQ', daily, df_15min,
            rs_percentile=75.0, sector_rs=65.0, regime_mult=1.0,
            strategy='SWING', daily_metrics=d_metrics, live_close=live_price,
        )

    # No override (e.g. backtests via orchestrator.py, which deliberately leaves
    # live_close=None) keeps today's exact pre-fix behavior - proves the fix is
    # opt-in, not a silent behavior change for callers that don't pass it.
    assert stale_signal['LTP'] == pytest.approx(stale_close, abs=0.01)
    # A live orchestrator/analyze call, which does pass live_close, must reflect
    # the fresh price instead of the frozen Discovery-time one.
    assert live_signal['LTP'] == pytest.approx(live_price, abs=0.01)
    assert live_signal['LTP'] != stale_signal['LTP']


def test_earnings_veto_fires_for_event_one_day_out():
    """Regression guard for the earnings-veto gap: an event 1 day out is more
    imminent than the 3-4 day-out events that were already being caught, so it
    must trigger the same HIGH RISK veto, not fall through unflagged."""
    daily, df_15min = _make_fixture()
    scanner = HybridScanner()
    d_metrics = scanner.compute_daily_metrics(daily)
    one_day_out = {"Earnings_Date": "2023-11-01", "Days_To_Earnings": 1, "Earnings_Risk": "HIGH RISK 🛑 (Imminent Earnings)"}

    with patch.object(scanner.earnings_engine, 'evaluate_risk', return_value=one_day_out):
        signal = scanner.scan(
            'TESTSTOCK-EQ', daily, df_15min,
            rs_percentile=75.0, sector_rs=65.0, regime_mult=1.0,
            strategy='SWING', daily_metrics=d_metrics,
        )

    assert signal['Execution_Recommendation'] == 'AVOID (EARNINGS)'
    assert signal['Strength'] == 'EVENT RISK 🛑'


def test_earnings_unknown_is_flagged_not_silently_safe():
    """Regression guard for the CYIENTDLM-class gap: when the earnings data
    source (Yahoo Finance) has no date for a symbol, that must be visibly
    distinct from a genuinely cleared/SAFE result, not a silent pass-through -
    UNKNOWN doesn't hard-veto (most UNKNOWN symbols likely have no real event),
    but it must leave a visible trace so it can be manually checked."""
    daily, df_15min = _make_fixture()
    scanner = HybridScanner()
    d_metrics = scanner.compute_daily_metrics(daily)
    unknown = {"Earnings_Date": "Unknown", "Days_To_Earnings": 999, "Earnings_Risk": "UNKNOWN ❓ (No data - verify manually before entry)"}

    with patch.object(scanner.earnings_engine, 'evaluate_risk', return_value=unknown):
        signal = scanner.scan(
            'TESTSTOCK-EQ', daily, df_15min,
            rs_percentile=75.0, sector_rs=65.0, regime_mult=1.0,
            strategy='SWING', daily_metrics=d_metrics,
        )

    # Not a hard veto...
    assert signal['Strength'] != 'EVENT RISK 🛑'
    assert signal['Execution_Recommendation'] != 'AVOID (EARNINGS)'
    # ...but must not read identically to a genuinely cleared result either.
    assert 'verify manually' in signal['AI_Summary'].lower()


def test_earnings_analyzer_flags_one_day_out_event():
    """Direct unit test of EarningsAnalyzer's own date-threshold math (not just
    scanner_engine's veto wiring): proves there's no off-by-one in the
    danger-zone comparison for a near-term (1 day out) event specifically,
    since that was the original bug hypothesis before the real root cause
    (Yahoo Finance has no earnings date at all for some symbols) was found."""
    analyzer = EarningsAnalyzer(danger_zone_days=5)
    tomorrow = (datetime.now().date() + timedelta(days=1))
    with patch.object(analyzer, '_fetch_earnings_date', return_value=tomorrow):
        result = analyzer.evaluate_risk('ANYSTOCK')

    assert result['Days_To_Earnings'] == 1
    assert "HIGH RISK" in result['Earnings_Risk']


def test_earnings_analyzer_does_not_permanently_cache_unknown():
    """A None result (data source had nothing, or a transient failure) must not
    be cached - otherwise one bad/empty lookup silently and permanently
    suppresses the veto for that symbol for the scanner instance's lifetime."""
    analyzer = EarningsAnalyzer(danger_zone_days=5)
    with patch.object(analyzer, '_fetch_earnings_date', return_value=None) as mock_fetch:
        analyzer.evaluate_risk('ANYSTOCK')
        analyzer.evaluate_risk('ANYSTOCK')
    assert mock_fetch.call_count == 2  # not short-circuited by a cached None
    assert 'ANYSTOCK' not in analyzer.earnings_cache

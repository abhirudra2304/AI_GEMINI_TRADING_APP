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
from orchestrator import _log_topn_diff
from cache import _topn_history_path
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
    # Three prior days at the same 5 candle times as "today" below, so the
    # bucketed volume-baseline helpers (_bucketed_volume_baseline /
    # _bucketed_daily_volume_ratio) have same-time-of-day history to compare
    # against instead of silently falling back to the expected-volume floor.
    # 20 total rows stays under analyze_mtf_breakout's own len(df_15m) >= 21
    # requirement, so the MTF-override path's behavior (short-circuits to a
    # no-op below that threshold) is unaffected by this addition.
    prior_days_15min = pd.DataFrame({
        'Timestamp': pd.concat([
            pd.Series(pd.date_range(start=f'2023-10-{day} 09:15', periods=5, freq='15min'))
            for day in (25, 26, 27)
        ], ignore_index=True),
        'Open': [395.0, 395.5, 396.0, 396.2, 396.4] * 3,
        'High': [395.6, 396.1, 396.3, 396.5, 396.8] * 3,
        'Low': [394.8, 395.3, 395.8, 396.0, 396.2] * 3,
        'Close': [395.5, 396.0, 396.2, 396.4, 396.6] * 3,
        'Volume': [40000, 42000, 45000, 48000, 50000] * 3,
    })
    today_15min = pd.DataFrame({
        'Timestamp': pd.date_range(start='2023-10-28 09:15', periods=5, freq='15min'),
        'Open': [398.0, 398.5, 399.0, 399.2, 399.4],
        'High': [398.6, 399.1, 399.3, 399.5, 399.8],
        'Low': [397.8, 398.3, 398.8, 399.0, 399.2],
        'Close': [398.5, 399.0, 399.2, 399.4, 399.6],
        'Volume': [50000, 55000, 60000, 90000, 120000],
    })
    df_15min = pd.concat([prior_days_15min, today_15min], ignore_index=True)
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
    # Score/Vol_Ratio/BTST_Score pinned values updated 2026-07-30: Vol_Ratio now
    # uses a bucketed same-time-of-day baseline (median volume in the same
    # 15-min bucket over the last 3-5 prior sessions) instead of a time-blind
    # tail(40) median, which mixed candles from every time of day across
    # multiple days with no alignment - see scanner_engine.py's
    # _bucketed_volume_baseline/_bucketed_daily_volume_ratio. The fixture's
    # three added prior days (see _make_fixture) give the new baseline real
    # same-bucket history instead of falling back to the expected-volume floor.
    assert signal['Score'] == pytest.approx(80.4, abs=0.05)
    # Strength pinned value updated 2026-07-30 (regime-multiply-before-tiering
    # redesign): quality_score no longer has regime_mult_clamped applied before
    # tiering (moved to `quantity` sizing instead - see scanner_engine.py), and
    # STRONG/VERY STRONG merged into a single STRONG+ tier at raw_score>=89.1
    # pending real BULLISH/NEUTRAL-regime anchor data. This fixture uses
    # regime_mult=1.0, so quality_score is numerically unchanged (80.4) - only
    # the tier boundaries moved, landing 80.4 in the merged MODERATE band
    # [74.9, 89.1) instead of the old separate VERY STRONG band (>=80).
    assert signal['Strength'] == 'MODERATE ⚡'
    assert signal['BTST_Score'] == pytest.approx(80.53, abs=0.05)
    assert signal['Vol_Ratio'] == pytest.approx(1.89, abs=0.01)
    assert signal['Breakout250'] == 'YES'
    assert signal['Stop'] == pytest.approx(396.75, abs=0.01)
    assert signal['Target'] == pytest.approx(402.38, abs=0.01)
    assert signal['Risk_Reward'] == pytest.approx(1.5, abs=0.01)

    # Decision_Score/BTST_Final_Score pinned values updated 2026-07-29: Sector_RS
    # is no longer cooled/inverted in add_decision_scores (see utils.py) -
    # sector rotation persists rather than mean-reverts, so fading sector
    # leadership was the wrong call. sector_rs=65.0 in the fixture above shifts
    # Decision_Score by exactly +4.5 (= 65.0 * 0.15 - (100-65.0) * 0.15).
    # Re-updated 2026-07-30 alongside the Score change above (score_cool feeds
    # into Decision_Score too).
    ranked = add_decision_scores(pd.DataFrame([signal]))
    assert ranked.iloc[0]['Decision_Score'] == pytest.approx(26.6, abs=0.05)
    assert ranked.iloc[0]['BTST_Final_Score'] == pytest.approx(59.0, abs=0.05)


def test_swing_scoring_is_pinned():
    signal = _scan('SWING')
    # See test_btst_scoring_is_pinned above for why Score/Vol_Ratio changed 2026-07-30.
    assert signal['Score'] == pytest.approx(80.4, abs=0.05)
    # See test_btst_scoring_is_pinned above for why Strength changed 2026-07-30
    # (regime-multiply-before-tiering redesign, STRONG/VERY STRONG merged).
    assert signal['Strength'] == 'MODERATE ⚡'
    assert signal['Vol_Ratio'] == pytest.approx(1.89, abs=0.01)
    assert signal['Breakout250'] == 'YES'
    assert signal['Stop'] == pytest.approx(396.0, abs=0.01)
    assert signal['Target'] == pytest.approx(405.0, abs=0.01)
    assert signal['Risk_Reward'] == pytest.approx(2.0, abs=0.01)

    # See test_btst_scoring_is_pinned above for why Decision_Score's pinned
    # value changed (Sector_RS is no longer cooled/inverted, 2026-07-29;
    # Score/Vol_Ratio bucketing, 2026-07-30).
    ranked = add_decision_scores(pd.DataFrame([signal]))
    assert ranked.iloc[0]['Decision_Score'] == pytest.approx(26.6, abs=0.05)
    # SWING carries no BTST_Score, so BTST_Final_Score falls back to the
    # Execution_Score-based legacy formula in add_decision_scores.
    assert ranked.iloc[0]['BTST_Final_Score'] == pytest.approx(18.6, abs=0.05)


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


def test_earnings_unknown_with_short_history_is_hard_vetoed():
    """Regression guard for the new-ticker blind spot (2026-07-30): an
    established stock with an UNKNOWN earnings date (thin analyst coverage,
    e.g. CYIENTDLM) gets the soft "verify manually" flag - see
    test_earnings_unknown_is_flagged_not_silently_safe. But a ticker with too
    little price history to have earned that benefit of the doubt (a
    genuinely new/recent listing) must be hard-vetoed instead, since
    "probably no real event" isn't a safe assumption with no track record."""
    analyzer = EarningsAnalyzer(danger_zone_days=5, new_listing_history_days=65)
    with patch.object(analyzer, '_fetch_earnings_date', return_value=None):
        short_history_result = analyzer.evaluate_risk('NEWSTOCK', history_days=60)
        established_result = analyzer.evaluate_risk('OLDSTOCK', history_days=300)
        unknown_history_result = analyzer.evaluate_risk('UNFETCHABLESTOCK', history_days=None)

    assert "HIGH RISK" in short_history_result['Earnings_Risk']
    assert "New/Recent Listing" in short_history_result['Earnings_Risk']

    # The established-coverage-gap case must be unaffected - still soft-flagged.
    assert "HIGH RISK" not in established_result['Earnings_Risk']
    assert "UNKNOWN" in established_result['Earnings_Risk']

    # history_days=None (price data missing/unfetchable) must not get *less*
    # scrutiny than a ticker with confirmed short history - also hard-vetoed.
    assert "HIGH RISK" in unknown_history_result['Earnings_Risk']
    assert "New/Recent Listing" in unknown_history_result['Earnings_Risk']


def test_scan_hard_vetoes_short_history_unknown_earnings():
    """End-to-end check that scan() actually applies the new hard veto (not
    just EarningsAnalyzer in isolation) and surfaces the distinct new-listing
    message rather than the generic 'Earnings in N days' text, which would be
    nonsensical here since there's no known earnings date at all."""
    rows = 60  # above compute_daily_metrics' own 50-row floor, below the new 65-day threshold
    closes = [100.0 + i for i in range(rows)]
    daily = pd.DataFrame({
        'Timestamp': pd.date_range(start='2023-08-01', periods=rows, freq='D'),
        'Open': [c - 0.3 for c in closes],
        'High': [c + 0.5 for c in closes],
        'Low': [c - 1.0 for c in closes],
        'Close': closes,
        # Higher than _make_fixture()'s 500000: at this fixture's lower close
        # prices (~100-160 vs. _make_fixture's ~100-400), 500000 would land
        # Avg_Traded_Value_20d under the HIGH-liquidity hard filter's 100M
        # threshold and get this signal silently dropped before ever reaching
        # the earnings veto this test exists to check.
        'Volume': [1000000] * rows,
    })
    _, df_15min = _make_fixture()

    scanner = HybridScanner()
    d_metrics = scanner.compute_daily_metrics(daily)
    assert d_metrics is not None  # sanity check: 60 rows clears the 50-row floor

    new_listing_unknown = {
        "Earnings_Date": "Unknown", "Days_To_Earnings": 0,
        "Earnings_Risk": "HIGH RISK 🛑 (New/Recent Listing - No Earnings History To Verify Safety)",
    }
    with patch.object(scanner.earnings_engine, 'evaluate_risk', return_value=new_listing_unknown):
        signal = scanner.scan(
            'NEWSTOCK-EQ', daily, df_15min,
            rs_percentile=75.0, sector_rs=65.0, regime_mult=1.0,
            strategy='SWING', daily_metrics=d_metrics,
        )

    assert signal['Execution_Recommendation'] == 'AVOID (EARNINGS)'
    assert signal['Strength'] == 'EVENT RISK 🛑'
    assert 'no earnings history' in signal['AI_Summary'].lower()
    assert 'earnings in 0 days' not in signal['AI_Summary'].lower()  # not the generic days-based message


def test_topn_diff_attributes_phase2_drop_correctly(caplog):
    """Regression guard for the disappearing-top-stock blind spot (2026-07-30):
    a ticker present in run 1's top-N that fails a Phase 2 hard filter (e.g.
    RS threshold) on run 2 - still in the discovery watchlist, but scan()
    rejected it - must be logged as DROPPED with the correct attribution,
    not silently vanish from the report."""
    strategy = "TESTDROP"
    history_path = _topn_history_path(strategy)
    if os.path.exists(history_path):
        os.remove(history_path)  # stale file from a previous failed run

    try:
        # --- Run 1: DROPTEST is a solid top signal, plus one filler ticker. ---
        discovered_run1 = pd.DataFrame({'Symbol': ['DROPTEST', 'FILLER']})
        signals_run1 = pd.DataFrame({
            'Symbol': ['DROPTEST', 'FILLER'],
            'Decision_Score': [72.0, 60.0],
            'Strength': ['MODERATE ⚡', 'MODERATE ⚡'],
            'Execution_Recommendation': ['N/A', 'N/A'],
        })
        with caplog.at_level('INFO'):
            _log_topn_diff(signals_run1, discovered_run1, strategy, 'Decision_Score')
        assert 'no prior baseline' in caplog.text.lower()
        assert os.path.exists(history_path)

        # --- Run 2: DROPTEST still in the discovery watchlist (Phase 1 kept
        # it) but failed scan()'s RS-threshold hard filter (Phase 2), so it
        # never made it into this run's confirmed signals at all. ---
        caplog.clear()
        discovered_run2 = pd.DataFrame({'Symbol': ['DROPTEST', 'FILLER']})
        signals_run2 = pd.DataFrame({
            'Symbol': ['FILLER'],
            'Decision_Score': [61.0],
            'Strength': ['MODERATE ⚡'],
            'Execution_Recommendation': ['N/A'],
        })
        with caplog.at_level('INFO'):
            _log_topn_diff(signals_run2, discovered_run2, strategy, 'Decision_Score')

        assert 'DROPPED' in caplog.text
        assert 'DROPTEST' in caplog.text
        assert 'Failed Phase 2 confirmation scan' in caplog.text
        # FILLER stayed in the top-N with only minor drift - must not be misreported as dropped/demoted.
        assert 'FILLER' not in caplog.text.split('DROPPED:')[1].split('\n')[0]
    finally:
        if os.path.exists(history_path):
            os.remove(history_path)


def test_topn_diff_attributes_phase1_drop_correctly(caplog):
    """A ticker missing from discovered_df entirely (Rank_Score fell below the
    Phase 1 cutoff, or it failed discovery.py's own hard filter) must be
    attributed to Phase 1, not conflated with a Phase 2 confirmation failure."""
    strategy = "TESTDROP2"
    history_path = _topn_history_path(strategy)
    if os.path.exists(history_path):
        os.remove(history_path)

    try:
        discovered_run1 = pd.DataFrame({'Symbol': ['DROPTEST', 'FILLER']})
        signals_run1 = pd.DataFrame({
            'Symbol': ['DROPTEST', 'FILLER'],
            'Decision_Score': [72.0, 60.0],
            'Strength': ['MODERATE ⚡', 'MODERATE ⚡'],
            'Execution_Recommendation': ['N/A', 'N/A'],
        })
        with caplog.at_level('INFO'):
            _log_topn_diff(signals_run1, discovered_run1, strategy, 'Decision_Score')

        caplog.clear()
        # DROPTEST isn't in this run's discovery watchlist at all.
        discovered_run2 = pd.DataFrame({'Symbol': ['FILLER']})
        signals_run2 = pd.DataFrame({
            'Symbol': ['FILLER'],
            'Decision_Score': [61.0],
            'Strength': ['MODERATE ⚡'],
            'Execution_Recommendation': ['N/A'],
        })
        with caplog.at_level('INFO'):
            _log_topn_diff(signals_run2, discovered_run2, strategy, 'Decision_Score')

        assert 'DROPTEST' in caplog.text
        assert 'Dropped from discovery watchlist (Phase 1)' in caplog.text
    finally:
        if os.path.exists(history_path):
            os.remove(history_path)

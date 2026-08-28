import pandas as pd
import os
import sys

# Add the project root to the Python path to resolve import issues
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from scanner_engine import HybridScanner
from utils import install_and_import

pytest = install_and_import('pytest')


def test_compute_daily_metrics_rsi():
    """
    Validates the RSI calculation within compute_daily_metrics against a known
    data series to ensure indicator accuracy.
    """
    scanner = HybridScanner()
    # A known series for RSI calculation, long enough to avoid initialization effects.
    close_prices = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
        45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
        46.27, 46.9, 47.57, 47.59, 47.2, 47.22, 46.65, 46.35, 46.94, 46.58,
        46.9, 47.2, 47.1, 47.4, 47.2, 47.1, 46.5, 46.6, 47.2, 47.3,
        47.1, 47.0, 47.2, 47.5, 47.3, 47.2, 47.0, 47.2, 47.3, 47.5
    ]
    # Create a DataFrame with enough data for all indicators to compute.
    df = pd.DataFrame({
        'Timestamp': pd.to_datetime(pd.date_range(start='2023-01-01', periods=len(close_prices))),
        'Open': [p - 0.1 for p in close_prices],
        'High': [p + 0.1 for p in close_prices],
        'Low': [p - 0.2 for p in close_prices],
        'Close': close_prices,
        'Volume': [1000] * len(close_prices)
    })

    metrics = scanner.compute_daily_metrics(df)
    assert metrics is not None, "Metrics calculation should not fail on valid data"
    # This series is the classic Wilder/Investopedia RSI walkthrough (first 15 closes),
    # extended with 35 more days of continued uptrend so it clears compute_daily_metrics'
    # 50-row minimum. The textbook ~66.32 value is RSI at day 15 of that walkthrough, not
    # the value after 35 additional days of EWM smoothing - by day 50 the correct Wilder
    # RSI has moved on. 57.30 is verified two independent ways: pandas-ta's ta.rsi() on
    # this exact series, and a hand-rolled iterative Wilder RMA loop with no pandas .ewm()
    # involved at all - both land on 57.300022754248545.
    assert 57.0 < metrics['RSI'] < 57.6, f"RSI value {metrics['RSI']} is outside the expected range."


def test_intraday_rsi_matches_daily_rsi_formula():
    """
    _intraday_rsi (used by compute_intraday_execution_score for GAP/INTRADAY strategies)
    shares the exact same Wilder-RSI formula as compute_daily_metrics, so it must land on
    the same corrected value (57.300022754248545) for the same known series - guards
    against the two RSI implementations drifting apart again after the leading-NaN fix.
    """
    scanner = HybridScanner()
    close_prices = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
        45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
        46.27, 46.9, 47.57, 47.59, 47.2, 47.22, 46.65, 46.35, 46.94, 46.58,
        46.9, 47.2, 47.1, 47.4, 47.2, 47.1, 46.5, 46.6, 47.2, 47.3,
        47.1, 47.0, 47.2, 47.5, 47.3, 47.2, 47.0, 47.2, 47.3, 47.5
    ]
    rsi = scanner._intraday_rsi(pd.Series(close_prices))
    assert 57.0 < rsi < 57.6, f"RSI value {rsi} is outside the expected range."
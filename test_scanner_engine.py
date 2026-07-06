import pandas as pd
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
    # The last RSI value for this series using standard EMA calculation is ~66.33
    assert 66.0 < metrics['RSI'] < 67.0, f"RSI value {metrics['RSI']} is outside the expected range."
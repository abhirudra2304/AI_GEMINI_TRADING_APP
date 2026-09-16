"""Regression tests for fetch_ohlcv's verify_settlement parameter (2026-09-16).

Built to fix real, measured AB1021 contention: market_regime.py and
extension_score.py were re-verifying VIX/index provisional bars against
settle time on every 1h cache-TTL lapse, for context data nobody reports as
a trigger/stop/target. verify_settlement=False lets a low-stakes caller
accept the cached provisional value instead of forcing a real API call.

The default (True) must reproduce the exact prior behavior - this is a V1.0
core file, so a regression here would silently reintroduce the settled-close
bug that d185104 fixed.
"""
import os
from datetime import datetime, timedelta

import pandas as pd
import pytest

import data_broker as db_module
from data_broker import DataBroker


def _write_cache(path, last_date, mtime, rows=5):
    dates = pd.date_range(end=last_date, periods=rows, freq='D')
    df = pd.DataFrame({
        'Timestamp': dates, 'Open': 100.0, 'High': 101.0, 'Low': 99.0,
        'Close': 100.0, 'Volume': 1000,
    })
    df.to_parquet(path)
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))
    return df


@pytest.fixture
def broker(tmp_path, monkeypatch):
    b = DataBroker.__new__(DataBroker)  # skip __init__ - no live session needed
    b.cache_dir = str(tmp_path)
    return b


def test_verify_settlement_false_accepts_provisional_bar(broker, monkeypatch):
    """A same-day row, file written BEFORE settle, TTL lapsed: with
    verify_settlement=False this must NOT trigger a re-fetch attempt."""
    today = datetime(2026, 9, 16, 14, 0)
    monkeypatch.setattr(db_module, 'datetime', _FixedNow)
    _FixedNow._NOW = today

    path = os.path.join(broker.cache_dir, "TESTSYM_ONE_DAY_10.parquet")
    # File written today but BEFORE 15:35 settle, and TTL (1h) has lapsed.
    _write_cache(path, last_date=today, mtime=today - timedelta(hours=2))

    called = {'api': False}
    def _boom(*a, **k):
        called['api'] = True
        raise AssertionError("should not attempt an API call when verify_settlement=False")
    monkeypatch.setattr(broker, '_call_rest_api', _boom, raising=False)
    monkeypatch.setattr(broker, '_resolve_symbol_row', _boom, raising=False)

    out = broker.fetch_ohlcv("TESTSYM", "ONE_DAY", 10, caller="test", verify_settlement=False)
    assert not called['api']
    assert not out.empty


def test_verify_settlement_true_still_forces_reverification(broker, monkeypatch):
    """Default behavior (True) must be unchanged: an unsettled same-day
    provisional bar past its TTL DOES attempt a re-fetch."""
    today = datetime(2026, 9, 16, 14, 0)
    monkeypatch.setattr(db_module, 'datetime', _FixedNow)
    _FixedNow._NOW = today

    path = os.path.join(broker.cache_dir, "TESTSYM2_ONE_DAY_10.parquet")
    _write_cache(path, last_date=today, mtime=today - timedelta(hours=2))

    attempted = {'lookup': False}
    def _lookup(*a, **k):
        attempted['lookup'] = True
        return None  # forces the "Symbol lookup failed" -> full-refresh fallback path
    monkeypatch.setattr(broker, '_resolve_symbol_row', _lookup, raising=False)

    def _no_session(*a, **k):
        raise ConnectionError("no live session in this test - expected, just proves the attempt happened")
    monkeypatch.setattr(broker, '_call_rest_api', _no_session, raising=False)

    try:
        broker.fetch_ohlcv("TESTSYM2", "ONE_DAY", 10, caller="test", verify_settlement=True)
    except Exception:
        pass  # the point is only that a re-verification attempt was made
    assert attempted['lookup'], "verify_settlement=True must still attempt to re-verify an unsettled bar"


def test_missing_day_still_fetched_regardless_of_verify_settlement(broker, monkeypatch):
    """verify_settlement must never excuse skipping a genuinely MISSING day -
    it only governs re-verification of an already-cached today's row."""
    today = datetime(2026, 9, 16, 14, 0)
    monkeypatch.setattr(db_module, 'datetime', _FixedNow)
    _FixedNow._NOW = today

    path = os.path.join(broker.cache_dir, "TESTSYM3_ONE_DAY_10.parquet")
    # Cache's last row is YESTERDAY, not today - a real gap, not a provisional bar.
    _write_cache(path, last_date=today - timedelta(days=1), mtime=today - timedelta(hours=2))

    attempted = {'lookup': False}
    def _lookup(*a, **k):
        attempted['lookup'] = True
        return None
    monkeypatch.setattr(broker, '_resolve_symbol_row', _lookup, raising=False)
    monkeypatch.setattr(broker, '_call_rest_api',
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("expected")),
                        raising=False)

    try:
        broker.fetch_ohlcv("TESTSYM3", "ONE_DAY", 10, caller="test", verify_settlement=False)
    except Exception:
        pass
    assert attempted['lookup'], "a missing day must still be fetched even with verify_settlement=False"


class _FixedNow(datetime):
    _NOW = None
    @classmethod
    def now(cls, tz=None):
        return cls._NOW.replace(tzinfo=tz) if tz else cls._NOW

"""A failed force refresh must not destroy the existing daily cache (2026-10-07: ~25 symbols lost
their file when AB1021 killed every refetch attempt, and the brief dropped them)."""
import os
import types

import pandas as pd
import pytest

import data_broker as db_module
from data_broker import DataBroker


def _write_cache(path, rows=30):
    dates = pd.date_range(end="2026-10-06", periods=rows, freq="D", tz="Asia/Kolkata")
    pd.DataFrame({"Timestamp": dates, "Open": 100.0, "High": 101.0, "Low": 99.0,
                  "Close": 100.0, "Volume": 1000}).to_parquet(path)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    b = DataBroker.__new__(DataBroker)
    b.cache_dir = str(tmp_path)
    b.api = types.SimpleNamespace(getCandleData=None)
    monkeypatch.setattr(b, "_enforce_api_rate_limit", lambda: None, raising=False)
    monkeypatch.setattr(b, "_resolve_symbol_row", lambda s: {"token": "1"}, raising=False)
    monkeypatch.setattr(db_module.time, "sleep", lambda s: None)
    return b


def _path(b):
    return os.path.join(b.cache_dir, "TESTSYM_ONE_DAY_400.parquet")


def test_failed_force_refresh_restores_and_serves_old_cache(broker, monkeypatch):
    _write_cache(_path(broker))

    def boom(*a, **k):
        raise Exception("Couldn't parse the JSON response: b'Access denied because of exceeding access rate'")
    monkeypatch.setattr(broker, "_call_rest_api", boom, raising=False)

    out = broker.fetch_ohlcv("TESTSYM", "ONE_DAY", 400, force_refresh=True, caller="test")
    assert not out.empty and len(out) == 30
    assert os.path.exists(_path(broker))
    assert not os.path.exists(_path(broker) + ".prev")


def test_failed_lookup_restores_old_cache(broker, monkeypatch):
    _write_cache(_path(broker))
    monkeypatch.setattr(broker, "_resolve_symbol_row", lambda s: None, raising=False)
    out = broker.fetch_ohlcv("TESTSYM", "ONE_DAY", 400, force_refresh=True, caller="test")
    assert len(out) == 30 and os.path.exists(_path(broker))


def test_successful_force_refresh_replaces_cache_and_drops_backup(broker, monkeypatch):
    _write_cache(_path(broker), rows=30)
    data = [[f"2026-09-{d:02d}T00:00:00+0530", 1, 2, 0.5, 1.5, 10] for d in range(1, 21)]
    monkeypatch.setattr(broker, "_call_rest_api", lambda *a, **k: {"status": True, "data": data}, raising=False)
    out = broker.fetch_ohlcv("TESTSYM", "ONE_DAY", 400, force_refresh=True, caller="test")
    assert len(out) == 20
    assert len(pd.read_parquet(_path(broker))) == 20
    assert not os.path.exists(_path(broker) + ".prev")


def test_no_cache_and_failed_fetch_returns_empty(broker, monkeypatch):
    def boom(*a, **k):
        raise Exception("exceeding access rate")
    monkeypatch.setattr(broker, "_call_rest_api", boom, raising=False)
    out = broker.fetch_ohlcv("TESTSYM", "ONE_DAY", 400, force_refresh=True, caller="test")
    assert out.empty and not os.path.exists(_path(broker))


def test_orphaned_backup_is_recovered_on_next_normal_call(broker, monkeypatch):
    _write_cache(_path(broker) + ".prev")
    assert not os.path.exists(_path(broker))
    monkeypatch.setattr(broker, "_call_rest_api",
                        lambda *a, **k: (_ for _ in ()).throw(Exception("exceeding access rate")), raising=False)
    broker.fetch_ohlcv("TESTSYM", "ONE_DAY", 400, caller="test")
    assert os.path.exists(_path(broker)) and not os.path.exists(_path(broker) + ".prev")

"""Regression tests for the two timing gates that failed live on 2026-09-10.

Both bugs shared a shape: something checked the WALL CLOCK and concluded the
data was fine, when the question was actually about the VINTAGE of the data.
"""
import datetime as dt
import pickle

import pandas as pd
import pytest

import config
import decision_brief as db
import momentum_scanner as ms


class _FixedNow(dt.datetime):
    """datetime subclass whose .now() is pinned; set _AT before use."""
    _AT = dt.datetime(2026, 9, 10, 14, 50)

    @classmethod
    def now(cls, tz=None):
        return cls._AT.replace(tzinfo=tz)


@pytest.fixture
def at_1450(monkeypatch):
    _FixedNow._AT = dt.datetime(2026, 9, 10, 14, 50)
    monkeypatch.setattr(db, 'datetime', _FixedNow)
    yield


def test_report_scan_time_parses_filename():
    assert db._report_scan_time('momentum_report_20260910_141028.csv') == \
        dt.datetime(2026, 9, 10, 14, 10, 28)
    assert db._report_scan_time('x/y/momentum_report_20260910_151107.csv') == \
        dt.datetime(2026, 9, 10, 15, 11, 7)
    assert db._report_scan_time(None) is None
    assert db._report_scan_time('no_timestamp_here.csv') is None


def test_pre_1430_report_is_not_endorsed_mid_window(at_1450):
    """The live bug: at 14:50 a 14:10 report got a green 'right time' tick."""
    note = db._timing_notes({'momentum_report': 'momentum_report_20260910_141028.csv'})[0]
    assert note.startswith('⚠️'), note
    assert '14:10' in note
    assert '--force-refresh' in note


def test_post_1430_report_is_endorsed_mid_window(at_1450):
    note = db._timing_notes({'momentum_report': 'momentum_report_20260910_143800.csv'})[0]
    assert note.startswith('✅'), note
    assert '14:38' in note


def test_unknown_vintage_is_not_endorsed(at_1450):
    """Unknown is not the same as good."""
    for meta in ({}, {'momentum_report': 'garbage.csv'}):
        assert db._timing_notes(meta)[0].startswith('⚠️')


def test_momentum_cache_discarded_across_the_1430_boundary(tmp_path, monkeypatch):
    """A pre-14:30 scan must not be served to a post-14:30 caller."""
    cache = tmp_path / 'momentum_cache.pkl'
    created = dt.datetime(2026, 9, 10, 14, 10, tzinfo=config.MARKET_TZ)
    cache.write_bytes(pickle.dumps({
        'created_at': created.isoformat(timespec='seconds'),
        'df': pd.DataFrame({'Symbol': ['X'], 'Rank': [1]}),
    }))
    monkeypatch.setattr(ms, 'MOMENTUM_CACHE_PATH', str(cache))

    class _MS(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 10, 14, 50, tzinfo=tz)

    monkeypatch.setattr(ms, 'datetime', _MS)
    assert ms._load_momentum_cache({'cache_ttl_minutes': 240}) is None


def test_momentum_cache_still_served_within_the_same_side(tmp_path, monkeypatch):
    """The gate must not break normal caching on one side of the boundary."""
    cache = tmp_path / 'momentum_cache.pkl'
    created = dt.datetime(2026, 9, 10, 14, 40, tzinfo=config.MARKET_TZ)
    cache.write_bytes(pickle.dumps({
        'created_at': created.isoformat(timespec='seconds'),
        'df': pd.DataFrame({'Symbol': ['X'], 'Rank': [1]}),
    }))
    monkeypatch.setattr(ms, 'MOMENTUM_CACHE_PATH', str(cache))

    class _MS(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 10, 14, 50, tzinfo=tz)

    monkeypatch.setattr(ms, 'datetime', _MS)
    out = ms._load_momentum_cache({'cache_ttl_minutes': 240})
    assert out is not None and len(out) == 1

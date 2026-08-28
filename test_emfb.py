import os
import sys
from datetime import datetime
from unittest.mock import patch

import pandas as pd

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import config
from emfb import (
    _check_breadth_regime,
    _check_index_regime,
    _get_market_regime_label,
    _is_scan_window,
)
from utils import install_and_import

pytest = install_and_import('pytest')


def _daily_df(prev_close: float, last_close: float) -> pd.DataFrame:
    return pd.DataFrame({'Close': [prev_close, last_close]})


# --- _check_index_regime ------------------------------------------------------

def test_index_regime_inactive_on_calm_market():
    data_store = {
        'Nifty 50': {'daily': _daily_df(100, 100.5)},          # +0.5%, above -0.8% threshold
        'NIFTY BANK': {'daily': _daily_df(100, 100.5)},
        'NIFTY MIDCAP 100': {'daily': _daily_df(100, 100.5)},
        'NIFTY SMALLCAP 100': {'daily': _daily_df(100, 100.5)},
        'India VIX': {'daily': pd.DataFrame({'Close': [12.0]})},  # below 18.0 threshold
    }
    active, reasons = _check_index_regime(data_store)
    assert active is False
    assert reasons == []


def test_index_regime_activates_on_nifty_weakness():
    data_store = {
        'Nifty 50': {'daily': _daily_df(100, 98.5)},  # -1.5%, breaches -0.8% threshold
        'India VIX': {'daily': pd.DataFrame({'Close': [12.0]})},
    }
    active, reasons = _check_index_regime(data_store)
    assert active is True
    assert any('Nifty' not in r and '50' in r or 'Nifty 50' in r for r in reasons) or reasons  # a reason was recorded


def test_index_regime_activates_on_high_vix():
    data_store = {
        'Nifty 50': {'daily': _daily_df(100, 100.5)},
        'India VIX': {'daily': pd.DataFrame({'Close': [25.0]})},  # above 18.0 threshold
    }
    active, reasons = _check_index_regime(data_store)
    assert active is True
    assert any('VIX' in r for r in reasons)


def test_index_regime_handles_missing_data_gracefully():
    active, reasons = _check_index_regime({})
    assert active is False
    assert reasons == []


# --- _check_breadth_regime -----------------------------------------------------

def test_breadth_regime_inactive_when_advancers_dominate():
    data_store = {f'STOCK{i}': {'daily': _daily_df(100, 101)} for i in range(8)}
    data_store.update({f'DECLINER{i}': {'daily': _daily_df(100, 99)} for i in range(2)})
    active, reasons = _check_breadth_regime(data_store)
    assert active is False


def test_breadth_regime_activates_on_poor_advance_decline_ratio():
    data_store = {f'STOCK{i}': {'daily': _daily_df(100, 101)} for i in range(2)}
    data_store.update({f'DECLINER{i}': {'daily': _daily_df(100, 99)} for i in range(8)})
    active, reasons = _check_breadth_regime(data_store)
    assert active is True
    assert reasons


def test_breadth_regime_ignores_core_indices():
    """CORE_INDICES entries must not be counted as universe advance/decline breadth."""
    data_store = {
        'Nifty 50': {'daily': _daily_df(100, 50)},  # a huge "decline" that must be excluded
        'STOCK0': {'daily': _daily_df(100, 101)},
        'STOCK1': {'daily': _daily_df(100, 101)},
    }
    active, reasons = _check_breadth_regime(data_store)
    assert active is False  # would be a 0-decliner / all-advancer universe if indices are excluded correctly


# --- _get_market_regime_label ---------------------------------------------------

def test_regime_label_bearish_on_vix_reason():
    assert _get_market_regime_label(["VIX (25.00) > 18.0"]) == 'BEARISH_MARKET'


def test_regime_label_bearish_on_smallcap_reason():
    assert _get_market_regime_label(["Smallcap (-2.00%) < -1.5%"]) == 'BEARISH_MARKET'


def test_regime_label_neutral_on_other_reasons():
    assert _get_market_regime_label(["Nifty 50 (-1.00%) < -0.8%"]) == 'NEUTRAL_MARKET'


def test_regime_label_default_with_no_reasons():
    assert _get_market_regime_label([]) == 'DEFAULT'


# --- _is_scan_window -------------------------------------------------------------

def test_scan_window_true_inside_14_30_to_18_00_on_weekday():
    with patch('emfb.datetime') as mock_dt:
        mock_dt.now.return_value = datetime(2024, 1, 2, 15, 0, tzinfo=config.MARKET_TZ)  # Tuesday
        assert _is_scan_window() is True


def test_scan_window_false_before_14_30():
    with patch('emfb.datetime') as mock_dt:
        mock_dt.now.return_value = datetime(2024, 1, 2, 10, 0, tzinfo=config.MARKET_TZ)  # Tuesday morning
        assert _is_scan_window() is False


def test_scan_window_false_on_weekend():
    with patch('emfb.datetime') as mock_dt:
        mock_dt.now.return_value = datetime(2024, 1, 6, 15, 0, tzinfo=config.MARKET_TZ)  # Saturday
        assert _is_scan_window() is False

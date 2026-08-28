"""
Unit and Integration Tests for Check Sheet Logger (test_check_sheet_logger.py)
"""

import os
import sys
import json
import sqlite3
from datetime import datetime
import pandas as pd
import numpy as np

project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import pytest
from check_sheet_logger import (
    CheckStatus,
    CheckPillar,
    CheckItem,
    CheckSheet,
    CheckSheetEvaluator,
    CheckSheetLogger,
)
from database import SignalDB


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def sample_daily_df():
    rows = 60
    closes = [100.0 + i * 1.5 for i in range(rows)]
    return pd.DataFrame({
        "Timestamp": pd.date_range("2026-01-01", periods=rows, freq="D"),
        "Open": [c - 0.5 for c in closes],
        "High": [c + 1.5 for c in closes],
        "Low": [c - 1.0 for c in closes],
        "Close": closes,
        "Volume": [1_000_000] * rows,
    })


@pytest.fixture
def sample_15m_df():
    rows = 25
    closes = [185.0 + i * 0.2 for i in range(rows)]
    return pd.DataFrame({
        "Timestamp": pd.date_range("2026-03-01 09:15", periods=rows, freq="15min"),
        "Open": [c - 0.1 for c in closes],
        "High": [c + 0.3 for c in closes],
        "Low": [c - 0.2 for c in closes],
        "Close": closes,
        "Volume": [50_000] * rows,
    })


@pytest.fixture
def sample_nifty_df():
    rows = 60
    closes = [22000.0 + i * 20 for i in range(rows)]
    return pd.DataFrame({
        "Timestamp": pd.date_range("2026-01-01", periods=rows, freq="D"),
        "Open": [c - 50 for c in closes],
        "High": [c + 100 for c in closes],
        "Low": [c - 80 for c in closes],
        "Close": closes,
        "Volume": [5_000_000] * rows,
    })


@pytest.fixture
def sample_signal():
    return {
        "Symbol": "HAL",
        "LTP": 4500.0,
        "Trigger": 4500.0,
        "entry": 4500.0,
        "Stop": 4380.0,
        "stop": 4380.0,
        "Target": 4740.0,
        "target": 4740.0,
        "Risk_Reward": 2.0,
        "Risk_Level": "LOW",
        "Score": 82.0,
        "Decision_Score": 76.5,
        "Strength": "VERY STRONG 🚀",
        "Trend": "BULL",
        "RS_Pctl": 88.0,
        "Sector_RS": 78.0,
        "ADX": 32.0,
        "RSI": 64.0,
        "Vol_Ratio": 2.4,
        "Breakout250": "YES",
        "_True_Breakout250": True,
        "Qty": 41,
        "Cap_Req": "₹1,84,500",
        "MTF_Triggered": True,
        "MTF_Breakout_Status": "YES",
        "Institutional_Score": 85.0,
        "Data_Stale": False,
    }


# --------------------------------------------------------------------------- #
# Unit Tests: CheckSheet Data Structures
# --------------------------------------------------------------------------- #

def test_check_item_serialization():
    item = CheckItem(
        name="Test Check",
        pillar="Test Pillar",
        status=CheckStatus.PASS,
        score=5.0,
        max_score=5.0,
        value="100",
        threshold=">= 50",
        message="Test pass message",
    )
    d = item.to_dict()
    assert d["name"] == "Test Check"
    assert d["status"] == "PASS"
    assert d["score"] == 5.0
    assert d["is_veto"] is False


def test_check_sheet_to_dict_and_json():
    cs = CheckSheet(
        symbol="TRENT",
        timestamp="2026-08-25T14:00:00",
        strategy="SWING",
        total_score=82.5,
        max_possible_score=100.0,
        percentage=82.5,
        verdict="PASSED 🚀 (High-Conviction Setup)",
        vetoes=[],
        pillar_scores={"P1": 15.0, "P2": 20.0},
        pillar_max_scores={"P1": 15.0, "P2": 20.0},
        items=[],
        trade_parameters={"LTP": 5000.0},
    )
    d = cs.to_dict()
    assert d["symbol"] == "TRENT"
    assert d["total_score"] == 82.5
    assert d["verdict"].startswith("PASSED")

    json_str = cs.to_json()
    parsed = json.loads(json_str)
    assert parsed["symbol"] == "TRENT"
    assert parsed["percentage"] == 82.5


# --------------------------------------------------------------------------- #
# Unit Tests: CheckSheetEvaluator Logic & 6 Pillars
# --------------------------------------------------------------------------- #

def test_evaluator_high_conviction_setup(sample_signal, sample_daily_df, sample_15m_df, sample_nifty_df):
    evaluator = CheckSheetEvaluator()
    daily_metrics = {
        "Close": 4500.0,
        "EMA50": 4200.0,
        "EMA20": 4350.0,
        "RSI": 64.0,
        "adx": 32.0,
        "Pivot_250": 4400.0,
        "Avg_Traded_Value_20d": 350_000_000.0,
        "Institutional_Score": 85.0,
    }

    cs = evaluator.evaluate(
        symbol="HAL",
        signal=sample_signal,
        daily_metrics=daily_metrics,
        df_15min=sample_15m_df,
        df_daily=sample_daily_df,
        nifty_df=sample_nifty_df,
        rs_percentile=88.0,
        sector_rs=78.0,
        regime_mult=0.95,
        strategy="SWING",
        earnings_info={"Earnings_Risk": "LOW", "Days_To_Earnings": 45},
        data_stale=False,
    )

    assert cs.symbol == "HAL"
    assert cs.total_score >= 75.0
    assert "PASSED" in cs.verdict
    assert len(cs.vetoes) == 0
    assert len(cs.items) >= 12

    # Verify all 6 pillars received points
    assert cs.pillar_scores[CheckPillar.MACRO.value] > 10.0
    assert cs.pillar_scores[CheckPillar.RELATIVE_STRENGTH.value] > 15.0
    assert cs.pillar_scores[CheckPillar.TECHNICAL_TREND.value] > 15.0
    assert cs.pillar_scores[CheckPillar.VOLUME_ACCUMULATION.value] > 10.0
    assert cs.pillar_scores[CheckPillar.BREAKOUT_SETUP.value] > 10.0
    assert cs.pillar_scores[CheckPillar.RISK_SAFETY.value] > 10.0


def test_evaluator_hard_veto_earnings(sample_signal, sample_daily_df, sample_15m_df, sample_nifty_df):
    evaluator = CheckSheetEvaluator()
    daily_metrics = {
        "Close": 4500.0,
        "EMA50": 4200.0,
        "EMA20": 4350.0,
        "RSI": 64.0,
        "adx": 32.0,
        "Pivot_250": 4400.0,
        "Avg_Traded_Value_20d": 350_000_000.0,
    }

    # Immediate earnings in 2 days -> triggers HARD VETO
    cs = evaluator.evaluate(
        symbol="HAL",
        signal=sample_signal,
        daily_metrics=daily_metrics,
        df_15min=sample_15m_df,
        df_daily=sample_daily_df,
        nifty_df=sample_nifty_df,
        rs_percentile=88.0,
        sector_rs=78.0,
        regime_mult=0.95,
        strategy="SWING",
        earnings_info={"Earnings_Risk": "HIGH", "Days_To_Earnings": 2},
        data_stale=False,
    )

    assert len(cs.vetoes) > 0
    assert "REJECTED" in cs.verdict
    assert "Safety Veto Triggered" in cs.verdict
    assert any("Earnings announcement in 2 days" in v for v in cs.vetoes)


def test_evaluator_hard_veto_stale_data(sample_signal, sample_daily_df, sample_15m_df, sample_nifty_df):
    evaluator = CheckSheetEvaluator()
    daily_metrics = {
        "Close": 4500.0,
        "EMA50": 4200.0,
        "EMA20": 4350.0,
        "RSI": 64.0,
        "adx": 32.0,
    }

    # Stale data flag -> triggers HARD VETO
    cs = evaluator.evaluate(
        symbol="HAL",
        signal=sample_signal,
        daily_metrics=daily_metrics,
        df_15min=sample_15m_df,
        df_daily=sample_daily_df,
        nifty_df=sample_nifty_df,
        rs_percentile=88.0,
        sector_rs=78.0,
        data_stale=True,
    )

    assert len(cs.vetoes) > 0
    assert "REJECTED" in cs.verdict
    assert any("Stale market data detected" in v for v in cs.vetoes)


def test_evaluator_weak_setup_score_breakdown():
    evaluator = CheckSheetEvaluator()
    daily_metrics = {
        "Close": 100.0,
        "EMA50": 110.0,  # Below 50 EMA
        "EMA20": 105.0,  # Below 20 EMA
        "RSI": 42.0,     # Weak RSI < 50
        "adx": 12.0,     # Low ADX < 20
        "Pivot_250": 130.0,
        "Avg_Traded_Value_20d": 10_000_000.0,  # Low liquidity (1 Cr)
    }

    cs = evaluator.evaluate(
        symbol="WEAKSTOCK",
        signal={"Vol_Ratio": 0.6},
        daily_metrics=daily_metrics,
        rs_percentile=25.0,  # Low RS
        sector_rs=30.0,     # Low Sector RS
        regime_mult=0.55,   # Weak regime
        strategy="SWING",
        data_stale=False,
    )

    assert cs.total_score < 45.0
    assert "REJECTED" in cs.verdict or "WATCHLIST" in cs.verdict
    assert cs.pillar_scores[CheckPillar.TECHNICAL_TREND.value] < 8.0


# --------------------------------------------------------------------------- #
# Integration Tests: Logger, Terminal Format, File Save, DB Logging
# --------------------------------------------------------------------------- #

def test_logger_terminal_formatting(sample_signal, sample_daily_df, sample_15m_df, sample_nifty_df):
    cs_logger = CheckSheetLogger()
    daily_metrics = {
        "Close": 4500.0,
        "EMA50": 4200.0,
        "EMA20": 4350.0,
        "RSI": 64.0,
        "adx": 32.0,
        "Pivot_250": 4400.0,
        "Avg_Traded_Value_20d": 350_000_000.0,
    }

    cs = cs_logger.evaluator.evaluate(
        symbol="HAL",
        signal=sample_signal,
        daily_metrics=daily_metrics,
        df_15min=sample_15m_df,
        df_daily=sample_daily_df,
        nifty_df=sample_nifty_df,
        rs_percentile=88.0,
        sector_rs=78.0,
        regime_mult=0.95,
        strategy="SWING",
    )

    term_output = cs_logger.format_terminal(cs, verbose=True)
    assert "TRADE SETUP CHECK SHEET: HAL [SWING]" in term_output
    assert "PILLAR SCORECARD BREAKDOWN" in term_output
    assert "TRADE EXECUTION PARAMETERS" in term_output
    assert "DETAILED CHECKLIST ITEMS" in term_output
    assert "✅" in term_output


def test_logger_file_persistence(tmp_path, sample_signal, sample_daily_df):
    log_dir = str(tmp_path / "check_sheets")
    cs_logger = CheckSheetLogger(log_dir=log_dir)

    daily_metrics = {"Close": 1000.0, "EMA50": 950.0, "EMA20": 980.0, "RSI": 60.0, "adx": 28.0}
    cs = cs_logger.evaluator.evaluate(
        symbol="TRENT",
        signal=sample_signal,
        daily_metrics=daily_metrics,
        strategy="SWING",
    )

    saved_file = cs_logger.save_to_file(cs)
    assert saved_file is not None
    assert os.path.exists(saved_file)

    with open(saved_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["symbol"] == "TRENT"
    assert data["total_score"] == cs.total_score


def test_logger_db_persistence(tmp_path, sample_signal):
    db_file = str(tmp_path / "test_signals.db")
    db = SignalDB(db_path=db_file)

    cs_logger = CheckSheetLogger(db_path=db_file)
    daily_metrics = {"Close": 1000.0, "EMA50": 950.0, "EMA20": 980.0, "RSI": 60.0, "adx": 28.0}
    cs = cs_logger.evaluator.evaluate(
        symbol="BEL",
        signal=sample_signal,
        daily_metrics=daily_metrics,
        strategy="BTST",
    )

    cs_logger.log_to_db(cs)

    # Verify directly from SQLite
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, strategy, total_score, verdict, veto_count FROM check_sheet_logs WHERE symbol = 'BEL'")
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "BEL"
    assert row[1] == "BTST"
    assert row[2] == cs.total_score
    assert row[4] == 0

    db.close()

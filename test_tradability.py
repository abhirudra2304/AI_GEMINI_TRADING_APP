"""Offline tests for tradability.py and its decision-brief wiring (no NSE calls)."""
from unittest.mock import patch

import pandas as pd

import tradability
from tradability import annotate_tradability, build_status


def _status():
    series = pd.DataFrame({"SYMBOL": ["AAA", "BEE", "LT1", "LT4", "ST2", "GSMX"],
                           "SERIES": ["EQ", "BE", "EQ", "EQ", "EQ", "EQ"]})
    asm = [
        {"symbol": "LT1", "asmSurvIndicator": "Stage I", "survDesc": "Long Term Additional Surveillance Measure (LTASM) - Stage I"},
        {"symbol": "LT4", "asmSurvIndicator": "Stage IV", "survDesc": "Long Term Additional Surveillance Measure (LTASM) - Stage IV"},
        {"symbol": "ST2", "asmSurvIndicator": "Stage II", "survDesc": "Short Term Additional Surveillance Measure (STASM) - Stage II"},
    ]
    gsm = [{"symbol": "GSMX", "survDesc": "GSM stage 0"}]
    return build_status(series, asm, gsm).set_index("Symbol")


def test_flag_mapping():
    s = _status()["Tradability_Flag"]
    assert s["AAA"] == "OK"
    assert s["BEE"] == "NO_BTST"      # trade-for-trade series
    assert s["LT1"] == "CAUTION"      # ASM Stage I
    assert s["ST2"] == "CAUTION"      # ASM Stage II
    assert s["LT4"] == "NO_BTST"      # ASM Stage III+
    assert s["GSMX"] == "NO_BTST"     # any GSM stage


def test_surveillance_label_and_note():
    s = _status()
    assert s.loc["LT4", "Surveillance"] == "LTASM Stage 4"
    assert "trade-for-trade" in s.loc["BEE", "Tradability_Note"]


def test_unknown_when_status_missing_or_symbol_absent():
    df = pd.DataFrame({"Symbol": ["AAA", "ZZZ"]})
    assert (annotate_tradability(df, None)["Tradability_Flag"] == "UNKNOWN").all()
    out = annotate_tradability(df, _status().reset_index()).set_index("Symbol")["Tradability_Flag"]
    assert out["AAA"] == "OK" and out["ZZZ"] == "UNKNOWN"   # absent is never OK


def test_symbol_col_override():
    df = pd.DataFrame({"symbol": ["BEE"]})
    assert annotate_tradability(df, _status().reset_index(), symbol_col="symbol")["Tradability_Flag"].iloc[0] == "NO_BTST"


def test_load_status_failure_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(tradability, "CACHE_PATH", str(tmp_path / "none.json"))
    monkeypatch.setattr(tradability, "DELIVERY_CACHE", str(tmp_path / "nodir"))
    with patch.object(tradability, "_latest_series_from_nse", side_effect=RuntimeError("offline")):
        status, note = tradability.load_status(force=True)
    assert status is None and "UNKNOWN" in note


def test_brief_filter_drops_no_btst_only_when_asked():
    from decision_brief import _apply_section_filters
    df = pd.DataFrame({"Symbol": ["AAA", "BEE"], "Reliability_Flag": ["NEUTRAL"] * 2,
                       "Move_Stage": [""] * 2, "Trigger_Check": ["OK"] * 2,
                       "Tradability_Flag": ["OK", "NO_BTST"],
                       "Cache_Last_Bar": [pd.Timestamp.now().normalize()] * 2})
    kept, dropped, _ = _apply_section_filters(df, ["EXTENDED"], drop_no_btst=True)
    assert list(kept.Symbol) == ["AAA"] and any("NO_BTST" in k for k in dropped)
    kept2, dropped2, _ = _apply_section_filters(df, ["EXTENDED"])
    assert list(kept2.Symbol) == ["AAA", "BEE"] and not any("NO_BTST" in k for k in dropped2)

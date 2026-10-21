import pandas as pd
import liquidity as lq


def _day(path, rows):
    pd.DataFrame(rows, columns=["SYMBOL", "SERIES", "TURNOVER_LACS"]).to_parquet(path)


def test_tiers_and_unknown(tmp_path):
    for i in range(12):
        _day(tmp_path / f"2026100{i % 10}{i // 10}.parquet", [
            ("BIG", "EQ", 1_000_000.0),   # 10,000 cr
            ("MID", "EQ", 2_000.0),       # 20 cr
            ("THINNY", "EQ", 500.0),      # 5 cr
            ("TINY", "BE", 100.0),        # 1 cr
            ("NEW", "EQ", 100_000.0) if i < 3 else ("OTHER", "SM", 1.0),
        ])
    out = lq.load_liquidity(window=20, min_days=10, cache_dir=str(tmp_path)).set_index("Symbol")
    assert out.loc["BIG", "Liq_Flag"] == "OK"
    assert out.loc["MID", "Liq_Flag"] == "MODERATE"
    assert out.loc["THINNY", "Liq_Flag"] == "THIN"
    assert out.loc["TINY", "Liq_Flag"] == "ILLIQUID"
    assert out.loc["NEW", "Liq_Flag"] == "UNKNOWN"
    assert "OTHER" not in out.index  # SM series ignored


def test_annotate_keeps_all_rows_and_marks_missing(tmp_path):
    _day(tmp_path / "20261001.parquet", [("BIG", "EQ", 1_000_000.0)])
    liq = lq.load_liquidity(window=20, min_days=1, cache_dir=str(tmp_path))
    df = pd.DataFrame({"Symbol": ["BIG", "GHOST"]})
    out = lq.annotate_liquidity(df, liq)
    assert list(out["Symbol"]) == ["BIG", "GHOST"]
    assert list(out["Liq_Flag"]) == ["OK", "UNKNOWN"]


def test_annotate_without_data():
    out = lq.annotate_liquidity(pd.DataFrame({"Symbol": ["A"]}), None)
    assert out["Liq_Flag"].iloc[0] == "UNKNOWN"

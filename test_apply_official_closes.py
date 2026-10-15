import os

import pandas as pd

import apply_official_closes as aoc


def _cache(path, closes):
    ts = pd.date_range("2026-10-12", periods=len(closes), freq="D", tz="Asia/Kolkata")
    pd.DataFrame({"Timestamp": ts, "Open": closes, "High": closes, "Low": closes,
                  "Close": closes, "Volume": 1000}).to_parquet(path)
    os.utime(path, (1_700_000_000, 1_700_000_000))


def test_corrects_small_gap_keeps_adjusted_bars_and_mtime(tmp_path):
    p = str(tmp_path / "X_ONE_DAY_400.parquet")
    _cache(p, [100.0, 200.0, 50.0])
    d = pd.read_parquet(p)["Timestamp"].dt.date.tolist()
    official = {d[0]: 100.5,   # 0.5% off: last-trade vs official -> fix
                d[1]: 1000.0,  # 5x: split-adjusted history -> leave
                d[2]: 50.0}    # identical -> leave
    assert aoc.fix_file(p, official, dry_run=False) == 1
    out = pd.read_parquet(p)
    assert out["Close"].tolist() == [100.5, 200.0, 50.0]
    assert out["High"].iloc[0] == 100.5 and out["Low"].iloc[0] == 100.0
    assert os.stat(p).st_mtime == 1_700_000_000


def test_dry_run_changes_nothing(tmp_path):
    p = str(tmp_path / "X_ONE_DAY_400.parquet")
    _cache(p, [100.0])
    d = pd.read_parquet(p)["Timestamp"].dt.date.tolist()
    assert aoc.fix_file(p, {d[0]: 101.0}, dry_run=True) == 1
    assert pd.read_parquet(p)["Close"].tolist() == [100.0]

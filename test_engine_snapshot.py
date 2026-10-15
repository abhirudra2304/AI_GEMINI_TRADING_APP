from datetime import datetime

import pandas as pd

import config
import decision_brief as dbf
import engine_snapshot as es


def test_snapshot_refuses_to_start_inside_auction_window():
    late = datetime(2026, 10, 16, 15, 12, tzinfo=config.MARKET_TZ)
    assert es.snapshot(now=late) == ""


def test_brief_reads_latest_snapshot_for_strategy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    today = datetime.now(config.MARKET_TZ).strftime('%Y%m%d')
    old = pd.DataFrame({"Symbol": ["OLD"], "strategy": ["BTST"], "Engine_Score": [1.0],
                        "Trigger": [1.0], "Stop": [0.9], "Target": [1.2]})
    new = pd.DataFrame({"Symbol": ["LOW", "HIGH", "SW"], "strategy": ["BTST", "BTST", "SWING"],
                        "Engine_Score": [10.0, 90.0, 50.0], "Trigger": [1, 2, 3],
                        "Stop": [0.9, 1.8, 2.7], "Target": [1.2, 2.4, 3.6]})
    old.to_csv(f"engine_snapshot_{today}_143500.csv", index=False)
    new.to_csv(f"engine_snapshot_{today}_150900.csv", index=False)
    rows, stamp = dbf._load_todays_engine_snapshot("BTST")
    assert stamp == "15:09"
    assert rows["Symbol"].tolist() == ["HIGH", "LOW"]
    assert "strategy" not in rows.columns


def test_no_snapshot_today_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pd.DataFrame({"Symbol": ["X"], "strategy": ["BTST"], "Engine_Score": [1], "Trigger": [1],
                  "Stop": [1], "Target": [1]}).to_csv("engine_snapshot_20200101_150000.csv", index=False)
    rows, stamp = dbf._load_todays_engine_snapshot("BTST")
    assert rows.empty and stamp is None

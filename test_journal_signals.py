import sqlite3

import pandas as pd

import journal_signals as js
from explain_rank import explain_row


def test_target_hit():
    r = js.classify(100, 95, 110, [(111, 99, 108)])
    assert r["outcome"] == "TARGET" and r["r_multiple"] == 2.0


def test_stop_hit():
    r = js.classify(100, 95, 110, [(102, 94, 96)])
    assert r["outcome"] == "STOP" and r["r_multiple"] == -1.0


def test_both_touched_is_stop():
    assert js.classify(100, 95, 110, [(112, 94, 100)])["outcome"] == "STOP"


def test_no_fill():
    assert js.classify(100, 95, 110, [(99, 97, 98)])["outcome"] == "NO_FILL"


def test_timeout_and_open_and_invalid():
    assert js.classify(100, 95, 110, [(103, 98, 102)])["outcome"] == "TIMEOUT"
    assert js.classify(100, 95, 110, [])["outcome"] == "OPEN"
    assert js.classify(100, 105, 110, [(1, 1, 1)])["outcome"] == "INVALID"


def test_run_appends_once(tmp_path):
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE signals (timestamp TEXT, strategy TEXT, symbol TEXT, entry REAL, stop REAL, target REAL)")
    c.execute("INSERT INTO signals VALUES ('2026-11-20T14:30:00','BTST','AAA',100,95,110)")
    c.commit(); c.close()
    out = tmp_path / "signals.csv"
    bars = lambda s, d: [(111, 99, 108)]
    first = js.run("2026-11-23", db_path=str(db), out_path=str(out), bars_fn=bars)
    second = js.run("2026-11-23", db_path=str(db), out_path=str(out), bars_fn=bars)
    assert len(first) == 1 and second.empty
    assert len(pd.read_csv(out)) == 1


def test_explain_matches_formula():
    row = {"ADX": 30, "RS_Pctl": 90, "Sector_RS": 60, "RSI": 65, "Distance": 4.0}
    num = 30 * .4 + 90 * .8 + 60 * .4 + 65 * .2
    row["Rank_Score"] = num / max(1.0, 4.0 * 0.5)
    ex = explain_row(row)
    assert abs(ex["rank_recomputed"] - row["Rank_Score"]) < 1e-9
    assert not ex["volume_shock_inferred"]
    row["Rank_Score"] = num / max(1.0, 10.0 * 0.2)
    row["Distance"] = 10.0
    assert explain_row(row)["volume_shock_inferred"]


def test_partial_window_unfilled_is_open_not_no_fill():
    assert js.classify(100, 95, 110, [(99, 97, 98)], max_hold=10)["outcome"] == "OPEN"
    assert js.classify(100, 95, 110, [(99, 97, 98)] * 10, max_hold=10)["outcome"] == "NO_FILL"

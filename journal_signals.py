"""EOD pick-vs-outcome logger -> journal/signals.csv (used by /eod).

Usage: python journal_signals.py [--asof YYYY-MM-DD] [--dry-run]
For each BTST/SWING/GAP/INTRADAY/EMFB signal in signals.db dated before --asof
(default: today IST) that is not yet resolved in journal/signals.csv, replays
cached DAILY bars after the signal date and classifies it. Read-only on
signals.db and caches; the only write is the append to journal/signals.csv.

Daily-bar caveat: if one bar touches both stop and target the trade is booked
as STOP (pessimistic). Bars dated >= asof are ignored (may be provisional). GAP/INTRADAY
signals are scored on the NEXT daily bar (same-day bar mixes pre-signal moves). Entry is assumed filled only if a bar trades through it.
"""
import os
import sqlite3
import sys
from datetime import datetime

import pandas as pd

OUT_PATH = os.path.join("journal", "signals.csv")
COLUMNS = ["date", "strategy", "symbol", "entry", "stop", "target",
           "outcome", "exit_px", "r_multiple", "source"]
MAX_HOLD_BARS = {"BTST": 1, "GAP": 1, "INTRADAY": 1, "SWING": 10, "EMFB": 5}


def classify(entry, stop, target, bars, max_hold: int = 1) -> dict:
    """bars: iterable of (high, low, close) AFTER signal date. Long-only.

    Returns {outcome, exit_px, r_multiple}. outcome in
    TARGET | STOP | OPEN | NO_FILL | TIMEOUT | INVALID.
    """
    if entry is None or stop is None or target is None or entry <= stop or target <= entry:
        return {"outcome": "INVALID", "exit_px": None, "r_multiple": None}
    risk = entry - stop
    bars = list(bars)[:max_hold]
    if not bars:
        return {"outcome": "OPEN", "exit_px": None, "r_multiple": None}
    filled = False
    last_close = None
    for high, low, close in bars:
        last_close = close
        if not filled:
            if high < entry:
                continue
            filled = True
        if low <= stop:  # pessimistic: stop checked before target
            return {"outcome": "STOP", "exit_px": stop, "r_multiple": -1.0}
        if high >= target:
            return {"outcome": "TARGET", "exit_px": target, "r_multiple": round((target - entry) / risk, 3)}
    if len(bars) < max_hold:  # window not complete: may still fill / resolve
        return {"outcome": "OPEN", "exit_px": None, "r_multiple": None}
    if not filled:
        return {"outcome": "NO_FILL", "exit_px": None, "r_multiple": 0.0}
    if len(bars) >= max_hold:
        return {"outcome": "TIMEOUT", "exit_px": last_close, "r_multiple": round((last_close - entry) / risk, 3)}
    return {"outcome": "OPEN", "exit_px": None, "r_multiple": None}


def _bars_after(symbol: str, signal_date: str, before: str = None):
    from grind_scanner import _daily_cache_path
    path = _daily_cache_path(symbol)
    if not path:
        return None
    df = pd.read_parquet(path)
    if "Timestamp" not in df.columns:
        return None
    ts = pd.to_datetime(df["Timestamp"]).dt.tz_localize(None).dt.normalize()
    df = df.assign(_d=ts).sort_values("_d")
    df = df[df["_d"] > pd.Timestamp(signal_date)]
    if before:  # only settled bars: a bar dated >= asof may be provisional (mid-session fetch)
        df = df[df["_d"] < pd.Timestamp(before)]
    return list(zip(df["High"], df["Low"], df["Close"]))


def run(asof: str, dry_run: bool = False, db_path: str = "signals.db", out_path: str = OUT_PATH, bars_fn=_bars_after) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        sig = pd.read_sql(
            "SELECT date(timestamp) AS date, strategy, symbol, entry, stop, target FROM signals "
            "WHERE date(timestamp) < ? AND entry IS NOT NULL AND stop IS NOT NULL AND target IS NOT NULL "
            "AND date(timestamp) >= date(?, '-30 day')", conn, params=(asof, asof))
    finally:
        conn.close()
    sig = sig.drop_duplicates(["date", "strategy", "symbol"])
    done = set()
    if os.path.exists(out_path):
        prev = pd.read_csv(out_path)
        resolved = prev[prev["outcome"].isin(["TARGET", "STOP", "TIMEOUT", "NO_FILL", "INVALID"])]
        done = set(zip(resolved["date"], resolved["strategy"], resolved["symbol"]))
    rows = []
    for r in sig.itertuples(index=False):
        if (r.date, r.strategy, r.symbol) in done:
            continue
        bars = bars_fn(r.symbol, r.date, before=asof) if bars_fn is _bars_after else bars_fn(r.symbol, r.date)
        if bars is None:
            continue
        res = classify(r.entry, r.stop, r.target, bars, MAX_HOLD_BARS.get(str(r.strategy).upper(), 1))
        if res["outcome"] == "OPEN":
            continue
        rows.append({"date": r.date, "strategy": r.strategy, "symbol": r.symbol, "entry": r.entry,
                     "stop": r.stop, "target": r.target, **res, "source": "signals.db"})
    out = pd.DataFrame(rows, columns=COLUMNS)
    if not dry_run and not out.empty:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        out.to_csv(out_path, mode="a", header=not os.path.exists(out_path), index=False)
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    asof = args[args.index("--asof") + 1] if "--asof" in args else datetime.now().strftime("%Y-%m-%d")
    res = run(asof, dry_run="--dry-run" in args)
    print(res.to_string(index=False) if not res.empty else "no newly resolved signals")
    if not res.empty:
        print(f"\n{len(res)} resolved | TARGET {int((res.outcome == 'TARGET').sum())} | "
              f"STOP {int((res.outcome == 'STOP').sum())} | other {int((~res.outcome.isin(['TARGET', 'STOP'])).sum())}")

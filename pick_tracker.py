"""Standalone log + scorer for the top picks named in analysis sessions.

Not wired into main.py or any scanner - it only reads cached daily candles.

    python pick_tracker.py add SYMBOL TRIGGER STOP TARGET [--date YYYY-MM-DD] [--source TEXT] [--note TEXT]
    python pick_tracker.py score

Scoring rule (same for every pick):
  - Entry only if a session AFTER the flag date trades at/above Trigger.
    Never reaching it = NO_TRADE (not a loss).
  - After entry, first of Stop / Target hit on a daily low/high resolves it.
    A session that touches both counts as STOP (conservative - daily bars
    can't tell which came first).
  - Otherwise OPEN, marked to the latest cached close.
Candles come from the freshest historical_data/<SYM>_ONE_DAY*.parquet by last
bar date (never by row count - see grind_scanner's cache-selection bug).
"""
import argparse
import glob
import os
from datetime import date

import pandas as pd

LOG_PATH = "picks_log.csv"
COLUMNS = ["Flag_Date", "Symbol", "Trigger", "Stop", "Target", "Source", "Note"]


def _load_log() -> pd.DataFrame:
    if not os.path.exists(LOG_PATH):
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(LOG_PATH)


def _freshest_daily(symbol: str):
    best = None
    for f in glob.glob(os.path.join("historical_data", f"{symbol}_ONE_DAY*.parquet")):
        df = pd.read_parquet(f)
        if df.empty:
            continue
        key = (pd.to_datetime(df["Timestamp"]).max(), len(df))
        if best is None or key > best[0]:
            best = (key, df)
    if best is None:
        return None
    df = best[1].copy()
    df["D"] = pd.to_datetime(df["Timestamp"]).dt.tz_localize(None).dt.normalize()
    return df.sort_values("D")


def score_pick(row) -> dict:
    out = {"Entry_Date": "", "Status": "NO_DATA", "Exit_Date": "", "Return_Pct": None, "Last_Close": None}
    df = _freshest_daily(row["Symbol"])
    if df is None:
        return out
    after = df[df["D"] > pd.Timestamp(row["Flag_Date"])]
    out["Last_Close"] = float(df["Close"].iloc[-1])
    out["Status"] = "NO_TRADE"
    entry = None
    for _, r in after.iterrows():
        if entry is None:
            if r["High"] < row["Trigger"]:
                continue
            entry = r["D"]
            out["Entry_Date"] = entry.strftime("%Y-%m-%d")
        if r["Low"] <= row["Stop"]:
            out.update(Status="STOP", Exit_Date=r["D"].strftime("%Y-%m-%d"),
                       Return_Pct=100 * (row["Stop"] / row["Trigger"] - 1))
            return out
        if r["High"] >= row["Target"]:
            out.update(Status="TARGET", Exit_Date=r["D"].strftime("%Y-%m-%d"),
                       Return_Pct=100 * (row["Target"] / row["Trigger"] - 1))
            return out
    if entry is not None:
        out.update(Status="OPEN", Return_Pct=100 * (out["Last_Close"] / row["Trigger"] - 1))
    return out


def cmd_add(args):
    log = _load_log()
    new = {"Flag_Date": args.date, "Symbol": args.symbol.upper(), "Trigger": args.trigger,
           "Stop": args.stop, "Target": args.target, "Source": args.source, "Note": args.note}
    dup = (log["Flag_Date"] == new["Flag_Date"]) & (log["Symbol"] == new["Symbol"])
    if dup.any():
        print(f"Already logged: {new['Symbol']} on {new['Flag_Date']} - skipped.")
        return
    pd.concat([log, pd.DataFrame([new])], ignore_index=True).to_csv(LOG_PATH, index=False)
    print(f"Logged {new['Symbol']} {new['Flag_Date']}: trigger {args.trigger} / stop {args.stop} / target {args.target}")


def cmd_score(_args):
    log = _load_log()
    if log.empty:
        print("No picks logged yet.")
        return
    scored = pd.concat([log, log.apply(score_pick, axis=1, result_type="expand")], axis=1)
    pd.set_option("display.width", 200)
    cols = ["Flag_Date", "Symbol", "Trigger", "Stop", "Target", "Entry_Date", "Status", "Exit_Date", "Return_Pct", "Last_Close"]
    print(scored[cols].round(2).to_string(index=False))

    resolved = scored[scored["Status"].isin(["TARGET", "STOP"])]
    wins = (resolved["Status"] == "TARGET").sum()
    print("\n" + "-" * 70)
    print(f"Logged {len(scored)} | entered {scored['Entry_Date'].ne('').sum()} | "
          f"never triggered {(scored['Status'] == 'NO_TRADE').sum()} | open {(scored['Status'] == 'OPEN').sum()}")
    if len(resolved):
        print(f"Resolved {len(resolved)}: {wins} target / {len(resolved) - wins} stop -> "
              f"win rate {100 * wins / len(resolved):.1f}% | avg {resolved['Return_Pct'].mean():+.2f}% | "
              f"sum {resolved['Return_Pct'].sum():+.2f}%")
    if len(resolved) < 30:
        print(f"Sample is {len(resolved)} resolved trades - too small to conclude anything yet.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("symbol")
    a.add_argument("trigger", type=float)
    a.add_argument("stop", type=float)
    a.add_argument("target", type=float)
    a.add_argument("--date", default=date.today().isoformat())
    a.add_argument("--source", default="")
    a.add_argument("--note", default="")
    a.set_defaults(func=cmd_add)
    s = sub.add_parser("score")
    s.set_defaults(func=cmd_score)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

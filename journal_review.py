"""Period review over journal/ + signals.db + paper trackers. Writes journal/review_<date>.md.

    python journal_review.py              # last 30 calendar days
    python journal_review.py --days 7     # weekly
    python journal_review.py --since 2026-11-06
"""
import argparse
import glob
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

OUT = "journal"
RESOLVED = ("WIN", "LOSS", "TIME_STOP", "GAP_THROUGH_STOP")
RULE_START = "2026-11-06"   # research/btst_swing_reentry_rule.md


def _md(df):
    """Markdown table without the optional `tabulate` dependency."""
    if not len(df):
        return "_none_"
    cell = lambda v: "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def _closes(since):
    fs = [f for f in sorted(glob.glob("delivery_cache/2*.parquet")) if os.path.basename(f)[:8] >= since.strftime("%Y%m%d")]
    if not fs:
        return pd.DataFrame()
    X = pd.concat([pd.read_parquet(f, columns=["SYMBOL", "SERIES", "CLOSE_PRICE"]).assign(D=os.path.basename(f)[:8])
                   for f in fs])
    X = X[X.SERIES == "EQ"]
    C = X.pivot_table(index="D", columns="SYMBOL", values="CLOSE_PRICE")
    C.index = pd.to_datetime(C.index)
    return C


def section_market(dly):
    if not len(dly):
        return "_no journal rows_"
    a, b = dly.iloc[0], dly.iloc[-1]
    lines = [f"- Sessions journaled: {len(dly)} ({a.date} → {b.date})"]
    for k, name in [("nifty", "Nifty 50"), ("mid100", "Midcap 100"), ("small100", "Smallcap 100"), ("bank", "Bank"),
                    ("it", "IT"), ("defence", "Defence")]:
        if k in dly and dly[k].notna().sum() >= 2:
            s = dly[k].dropna()
            lines.append(f"- {name}: {s.iloc[0]:.0f} → {s.iloc[-1]:.0f} ({(s.iloc[-1] / s.iloc[0] - 1) * 100:+.1f}% over the journaled days)")
    if "vix" in dly:
        lines.append(f"- India VIX: {dly.vix.min():.1f}–{dly.vix.max():.1f}, last {dly.vix.iloc[-1]:.1f}")
    for k, name in [("fii_net", "FII net"), ("dii_net", "DII net")]:
        if k in dly:
            lines.append(f"- {name}: Rs {dly[k].sum():+,.0f} cr total; {(dly[k] > 0).sum()} buy days / {(dly[k] < 0).sum()} sell days")
    if "univ_pct_above_50dma" in dly:
        lines.append(f"- Universe % above 50DMA: {dly.univ_pct_above_50dma.iloc[0]:.0f}% → {dly.univ_pct_above_50dma.iloc[-1]:.0f}%")
    return "\n".join(lines)


def section_health(dly):
    if "brief_time" not in dly:
        return "_no decision-log data_"
    bt = pd.to_datetime(dly.brief_time.astype(str), format="%H:%M:%S", errors="coerce")
    ok = (bt.dt.hour * 60 + bt.dt.minute) < 15 * 60
    lines = [f"- Brief landed before 15:00 on {int(ok.sum())}/{bt.notna().sum()} days (latest {dly.brief_time.max()})",
             f"- Engine snapshot ran on {int(dly.engine_snapshot_ran.fillna(False).astype(bool).sum())}/{len(dly)} days",
             f"- Stale-served symbols per decision run: mean {dly.stale_served.mean():.1f}, max {dly.stale_served.max():.0f}"]
    return "\n".join(lines)


def section_strategies(since):
    c = sqlite3.connect("signals.db")
    o = pd.read_sql("select strategy, symbol, outcome, adj_return_pct, signal_timestamp from outcomes", c)
    o["ts"] = pd.to_datetime(o.signal_timestamp, errors="coerce", format="mixed")
    o = o[o.outcome.isin(RESOLVED)]
    out = []
    for strat in ("BTST", "SWING"):
        p = o[(o.strategy == strat) & (o.ts >= pd.Timestamp(since))]
        r = o[(o.strategy == strat) & (o.ts >= pd.Timestamp(RULE_START))].sort_values("ts")
        row = {"Strategy": strat, "Trades (period)": len(p),
               "Win % (period)": round((p.outcome == "WIN").mean() * 100, 1) if len(p) else None,
               "Avg net % (period)": round(p.adj_return_pct.mean(), 2) if len(p) else None}
        n = len(r)
        if n:
            w, l = r[r.adj_return_pct > 0].adj_return_pct, r[r.adj_return_pct <= 0].adj_return_pct
            be = abs(l.mean()) / (w.mean() + abs(l.mean())) * 100 if len(w) and len(l) else np.nan
            h = n // 2
            best = r.groupby("symbol").adj_return_pct.sum().idxmax()
            checks = {"n>=60": n >= 60, "mean>0": r.adj_return_pct.mean() > 0,
                      "win>breakeven": (r.adj_return_pct > 0).mean() * 100 > be,
                      "both halves>0": h > 0 and r.adj_return_pct.iloc[:h].mean() > 0 and r.adj_return_pct.iloc[h:].mean() > 0,
                      "ex-best>0": r[r.symbol != best].adj_return_pct.mean() > 0}
            row["Since rule: n / mean %"] = f"{n} / {r.adj_return_pct.mean():+.2f}"
            row["Re-entry"] = "EARNED BACK" if all(checks.values()) else "NOT YET (" + ", ".join(k for k, v in checks.items() if not v) + ")"
        else:
            row["Since rule: n / mean %"] = "0"
            row["Re-entry"] = "NOT YET (no resolved trades)"
        out.append(row)
    return _md(pd.DataFrame(out))


def section_events(ev, C):
    if not len(ev) or not len(C):
        return "_no events_"
    ew = C.pct_change(fill_method=None).mean(axis=1)
    rows = []
    for e in ev.itertuples():
        d = pd.Timestamp(e.date)
        if e.symbol not in C.columns or d not in C.index:
            continue
        i = C.index.get_loc(d)
        rec = {"date": e.date, "symbol": e.symbol, "type": e.type}
        for h in (5, 10, 20):
            if i + h < len(C):
                s = C[e.symbol].iloc[i + h] / C[e.symbol].iloc[i] - 1
                b = (1 + ew.iloc[i + 1:i + h + 1]).prod() - 1
                rec[f"x{h}d %"] = round((s - b) * 100, 2)
        rows.append(rec)
    R = pd.DataFrame(rows)
    num = [c for c in R.columns if c.startswith("x")]
    if not num:
        return f"{len(R)} events; too recent for forward returns yet.\n\n" + _md(R)
    summ = R.groupby("type")[num].agg(["count", "mean"]).round(2)
    summ.columns = [f"{a} {b}" for a, b in summ.columns]
    return ("Excess return vs the equal-weight market after each signal (positive = the signal helped):\n\n"
            + _md(summ.reset_index()) + "\n\n<details><summary>All events</summary>\n\n"
            + _md(R) + "\n\n</details>")


def section_holdings(hold):
    if not len(hold):
        return "_none_"
    g = hold.sort_values("date").groupby("symbol")
    t = pd.DataFrame({"group": g.group.last(), "first": g.close.first(), "last": g.close.last(),
                      "days": g.size(), "avgdown signals": g.avgdown.apply(lambda s: int(s.isin(["STRICT", "EARLY"]).sum())),
                      "delivery spikes": g.deliv_spike.apply(lambda s: int(s.fillna("").ne("").sum()))})
    t["change %"] = ((t["last"] / t["first"] - 1) * 100).round(1)
    return _md(t.reset_index().sort_values(["group", "change %"]))


def _cmd(args):
    try:
        r = subprocess.run([sys.executable] + args, capture_output=True, text=True, timeout=300)
        return "\n".join(l for l in r.stdout.splitlines() if l.strip())[-1500:]
    except Exception as e:
        return f"(failed: {e})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--since")
    a = ap.parse_args()
    since = pd.Timestamp(a.since) if a.since else pd.Timestamp(date.today() - timedelta(days=a.days))
    rd = lambda n: pd.read_csv(os.path.join(OUT, n)) if os.path.exists(os.path.join(OUT, n)) else pd.DataFrame()
    dly, hold, ev = rd("daily.csv"), rd("holdings.csv"), rd("events.csv")
    for df in (dly, hold, ev):
        if len(df):
            df.drop(df[pd.to_datetime(df.date) < since].index, inplace=True)
    C = _closes(since.date())
    md = [f"# Review {since.date()} → {date.today()}", "",
          "## Market", section_market(dly.sort_values("date") if len(dly) else dly), "",
          "## System health", section_health(dly), "",
          "## BTST / SWING (paper) and the re-entry rule", section_strategies(since), "",
          "## Watch signals and what followed", section_events(ev, C), "",
          "## Holdings and watch list", section_holdings(hold), "",
          "## Paper strategies (C1 / RS_CORR)", "```", _cmd(["strategy_paper.py", "score"]), "```", "",
          "## Pick tracker", "```", _cmd(["pick_tracker.py", "score"])[-600:], "```"]
    p = os.path.join(OUT, f"review_{date.today():%Y%m%d}.md")
    open(p, "w", encoding="utf-8").write("\n".join(md))
    print("\n".join(md))
    print(f"\nSaved {p}")


if __name__ == "__main__":
    main()

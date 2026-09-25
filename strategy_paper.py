"""Monthly PAPER tracking for the two strategies that passed the 2026-09-25 backtests.

Standalone and read-only against the rest of the system: reads/extends delivery_cache/ (NSE
sec_bhavdata_full files) and writes only paper_portfolio.csv. No broker API calls, no scanner changes.

    python strategy_paper.py update      # download any missing NSE delivery days
    python strategy_paper.py rebalance   # record new picks if due (every 21 sessions); --force to override
    python strategy_paper.py score       # each cohort's return vs the equal-weight universe benchmark

Strategies (rules frozen exactly as backtested - do NOT tune here without re-running the backtest):
  C1       : 12-1m momentum top 30% AND delivery-% surge (20d avg - 120d avg) top 30% of the
             point-in-time top-300-by-turnover universe; top 10 by rank-sum; hold 21 sessions.
  RS_CORR  : only when the universe equal-weight index is >=5% below its 60-session high AND below its
             50-session average: top 10 by 60-session excess return among names above their SMA50.
Cost 0.25% per round trip is deducted in `score`. ETFs are excluded via NSE's eq_etfseclist.
"""
import argparse
import hashlib
import io
import json
import os
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

from nse_announcements import NSE_HEADERS

CACHE = "delivery_cache"
LEDGER = "paper_portfolio.csv"
ETF_CACHE = os.path.join(CACHE, "_etf_list.csv")
MISSING = os.path.join(CACHE, "_missing.json")
COST = 0.0025
HOLD = 21
LOOKBACK_FILES = 320  # enough for 252d momentum + 60d turnover rank


# ----------------------------------------------------------------- data
def _file_hash(df: pd.DataFrame) -> str:
    return hashlib.md5(pd.util.hash_pandas_object(
        df[["SYMBOL", "CLOSE_PRICE", "TTL_TRD_QNTY"]], index=False).values).hexdigest()


def cmd_update(_args=None):
    os.makedirs(CACHE, exist_ok=True)
    missing = set(json.load(open(MISSING))) if os.path.exists(MISSING) else set()
    have = sorted(f[:8] for f in os.listdir(CACHE) if f.endswith(".parquet"))
    d = pd.Timestamp(have[-1]).date() + timedelta(days=1) if have else date(2020, 1, 1)
    prev_hash = _file_hash(pd.read_parquet(f"{CACHE}/{have[-1]}.parquet")) if have else None
    s = requests.Session()
    got = 0
    while d <= date.today():
        key = d.strftime("%Y%m%d")
        if d.weekday() < 5 and key not in missing:
            r = s.get(f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv",
                      headers=NSE_HEADERS, timeout=30)
            if r.status_code == 404:
                missing.add(key)
            elif r.status_code == 200:
                try:
                    df = pd.read_csv(io.StringIO(r.text)); df.columns = df.columns.str.strip()
                    df["SERIES"] = df["SERIES"].str.strip(); df["SYMBOL"] = df["SYMBOL"].str.strip()
                    df = df[df.SERIES.isin(["EQ", "BE"])].copy()
                    for c in ("DELIV_QTY", "DELIV_PER"):
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                    h = _file_hash(df)
                    if h == prev_hash:  # NSE serves the previous day's file on holidays
                        missing.add(key)
                    else:
                        df.drop(columns=["DATE1"]).to_parquet(f"{CACHE}/{key}.parquet", index=False)
                        prev_hash = h; got += 1
                except Exception:
                    print(f"{key}: non-CSV response, skipped (will retry next update)")
            time.sleep(0.4)
        d += timedelta(days=1)
    json.dump(sorted(missing), open(MISSING, "w"))
    print(f"update: {got} new day(s); latest {sorted(f for f in os.listdir(CACHE) if f.endswith('.parquet'))[-1][:8]}")


def _etfs() -> set:
    if not os.path.exists(ETF_CACHE) or time.time() - os.path.getmtime(ETF_CACHE) > 7 * 86400:
        try:
            r = requests.get("https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv",
                             headers=NSE_HEADERS, timeout=30)
            pd.read_csv(io.StringIO(r.text)).to_csv(ETF_CACHE, index=False)
        except Exception:
            pass
    return set(pd.read_csv(ETF_CACHE).Symbol.str.strip()) if os.path.exists(ETF_CACHE) else set()


def load(n_files=LOOKBACK_FILES, since=None):
    files = sorted(f for f in os.listdir(CACHE) if f.endswith(".parquet"))
    if since is not None:
        first = next((i for i, f in enumerate(files) if f[:8] >= since.strftime("%Y%m%d")), len(files))
        n_files = max(n_files, len(files) - first + 260)
    etf = _etfs(); fr = []
    for f in files[-n_files:]:
        d = pd.read_parquet(os.path.join(CACHE, f)); d = d[d.SERIES == "EQ"]
        d["D"] = pd.Timestamp(f[:8]); fr.append(d)
    X = pd.concat(fr)
    X = X[~X.SYMBOL.isin(etf) & ~X.SYMBOL.str.contains(r"(?:BEES|ETF|IETF)$", regex=True)]
    P = lambda c: X.pivot_table(index="D", columns="SYMBOL", values=c)
    C, PC, TO, DP = P("CLOSE_PRICE"), P("PREV_CLOSE"), P("TURNOVER_LACS"), P("DELIV_PER")
    ratio = PC / C.shift(1)
    fct = ratio.where((ratio - 1).abs() > 0.02, 1.0).fillna(1.0)
    adj = fct.iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
    return {"C": C, "CA": C * adj, "TO": TO, "DP": DP}


def signals(data):
    CA, TO, DP = data["CA"], data["TO"], data["DP"]
    U = TO.rolling(60, min_periods=40).median().rank(axis=1, ascending=False) <= 300
    m12 = CA.shift(21) / CA.shift(252) - 1
    surge = DP.rolling(20, min_periods=15).mean() - DP.rolling(120, min_periods=90).mean()
    s50 = CA.rolling(50, min_periods=45).mean()
    ew = CA.pct_change(fill_method=None).where(U.shift(1).fillna(False)).mean(axis=1).fillna(0)
    idx = (1 + ew).cumprod()
    corr = (idx <= 0.95 * idx.rolling(60).max()) & (idx < idx.rolling(50).mean())
    ex60 = (CA / CA.shift(60) - 1).sub(idx / idx.shift(60) - 1, axis=0)
    return U, m12, surge, s50, corr, ex60


# ----------------------------------------------------------------- commands
def _ledger():
    return pd.read_csv(LEDGER, parse_dates=["Rebalance_Date"]) if os.path.exists(LEDGER) else pd.DataFrame(
        columns=["Rebalance_Date", "Strategy", "Symbol", "Entry_Close", "Note"])


def cmd_rebalance(args):
    data = load(); CA, C = data["CA"], data["C"]
    U, m12, surge, s50, corr, ex60 = signals(data)
    d = CA.index[-1]; u = U.loc[d]; led = _ledger()
    last = led[led.Strategy == "C1"].Rebalance_Date.max() if len(led) else None
    sessions_since = (CA.index > last).sum() if last is not None and not pd.isna(last) else None
    if sessions_since is not None and sessions_since < HOLD and not args.force:
        print(f"Not due: {sessions_since}/{HOLD} sessions since the last C1 rebalance ({last:%Y-%m-%d}). Use --force to override.")
        return
    new = []
    a, b = m12.loc[d][u].rank(pct=True), surge.loc[d][u].rank(pct=True)
    c1 = (a + b)[(a >= 0.7) & (b >= 0.7)].dropna().nlargest(10).index
    new += [(d, "C1", s, C.loc[d, s], "") for s in c1]
    if corr.loc[d]:
        rs = ex60.loc[d][u & (CA.loc[d] > s50.loc[d])].dropna().nlargest(10).index
        new += [(d, "RS_CORR", s, C.loc[d, s], "correction active") for s in rs]
        print("Market is in a correction -> RS_CORR active.")
    else:
        print("No correction -> RS_CORR inactive this month (by design).")
    add = pd.DataFrame(new, columns=led.columns)
    pd.concat([led, add], ignore_index=True).to_csv(LEDGER, index=False)
    print(f"Recorded {len(add)} paper positions as of {d:%Y-%m-%d}:")
    print(add[["Strategy", "Symbol", "Entry_Close"]].to_string(index=False))


def cmd_score(_args):
    led = _ledger()
    if led.empty:
        print("Ledger empty - run `rebalance` first."); return
    data = load(since=led.Rebalance_Date.min()); CA = data["CA"]
    U = signals(data)[0]; idx = CA.index
    rows = []
    for (d, strat), g in led.groupby(["Rebalance_Date", "Strategy"]):
        if d not in idx:
            continue
        i = idx.get_loc(d); j = min(i + HOLD, len(idx) - 1); e = idx[j]
        r = (CA.loc[e, g.Symbol] / CA.loc[d, g.Symbol] - 1).mean() - COST
        bench = (CA.loc[e] / CA.loc[d] - 1)[U.loc[d]].dropna().mean()
        rows.append({"Rebalance": f"{d:%Y-%m-%d}", "Strategy": strat, "Names": len(g),
                     "Status": "CLOSED" if j == i + HOLD else f"OPEN ({j - i}/{HOLD})",
                     "Return%": 100 * r, "Bench%": 100 * bench, "Excess%": 100 * (r - bench)})
    R = pd.DataFrame(rows)
    print(R.round(2).to_string(index=False))
    for strat, g in R.groupby("Strategy"):
        print(f"\n{strat}: {len(g)} cohort(s), beat benchmark {int((g['Excess%'] > 0).sum())}/{len(g)}, "
              f"compounded {100 * ((1 + g['Return%'] / 100).prod() - 1):+.1f}% vs bench "
              f"{100 * ((1 + g['Bench%'] / 100).prod() - 1):+.1f}%")
    print("\nPaper only. Judge after 3-6 closed cohorts, not before.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update").set_defaults(func=cmd_update)
    r = sub.add_parser("rebalance"); r.add_argument("--force", action="store_true"); r.set_defaults(func=cmd_rebalance)
    sub.add_parser("score").set_defaults(func=cmd_score)
    a = p.parse_args(); a.func(a)


if __name__ == "__main__":
    main()

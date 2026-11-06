"""Institutional-accumulation event study. Rules: research/inst_accum_prereg_20261106.md
(fixed before the data was downloaded). Reads shp_cache/ and delivery_cache/; writes nothing
but research/inst_accum_events.csv.

    python research/inst_accum_test.py
"""
import glob
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import strategy_paper as sp  # noqa: E402

COST = 0.0025
H_PRIMARY, H_SECONDARY = 126, 63


def _dt(s, fmt):
    try:
        return datetime.strptime(s.strip(), fmt)
    except Exception:
        return None


def load_shp():
    rows = []
    for p in glob.glob("shp_cache/*.json"):
        sym = os.path.basename(p)[:-5]
        recs = []
        for r in json.load(open(p)):
            q = _dt(r.get("date") or "", "%d-%b-%Y")
            b = _dt((r.get("broadcast") or "")[:20], "%d-%b-%Y %H:%M:%S") or _dt((r.get("broadcast") or "")[:11], "%d-%b-%Y")
            if q is None or b is None:
                continue
            recs.append({"Symbol": sym, "q": pd.Timestamp(q), "b": pd.Timestamp(b), **{k: r.get(k) for k in
                         ("MF", "FPI", "DII", "MF_key", "FPI_key")}})
        if not recs:
            continue
        d = pd.DataFrame(recs).sort_values(["q", "b"]).drop_duplicates("q", keep="first")  # first filing = point in time
        d["dMF"] = d.MF.diff()
        same = d.FPI_key.eq(d.FPI_key.shift())
        d["dFPI"] = d.FPI.diff().where(same)
        d["prev_q"] = d.q.shift()
        rows.append(d.iloc[1:])
    ev = pd.concat(rows, ignore_index=True)
    ev = ev[(ev.q - ev.prev_q).dt.days.between(80, 100)]  # consecutive quarters only
    return ev


def main():
    files = sorted(f for f in os.listdir(sp.CACHE) if f.endswith(".parquet"))
    data = sp.load(n_files=len(files))
    CA, TO = data["CA"], data["TO"]
    U = TO.rolling(60, min_periods=40).median().rank(axis=1, ascending=False) <= 300
    idx = CA.index
    ret1 = CA.pct_change(fill_method=None).clip(-0.5, 1.0)

    ev = load_shp()
    out = []
    for r in ev.itertuples():
        i = idx.searchsorted(r.b.normalize(), side="right")  # first session strictly after broadcast date
        if i >= len(idx) or r.Symbol not in CA.columns or not U.iloc[i].get(r.Symbol, False):
            continue
        rec = {"Symbol": r.Symbol, "q": r.q, "entry": idx[i], "dMF": r.dMF, "dFPI": r.dFPI}
        for h in (H_SECONDARY, H_PRIMARY):
            if i + h >= len(idx):
                rec[f"x{h}"] = np.nan
                continue
            s = CA[r.Symbol].iloc[i + h] / CA[r.Symbol].iloc[i] - 1
            win = ret1.iloc[i + 1:i + h + 1]
            bench = (1 + win.where(U.iloc[i:i + h].values).mean(axis=1).fillna(0)).prod() - 1
            rec[f"x{h}"] = s - bench if np.isfinite(s) else np.nan
        out.append(rec)
    E = pd.DataFrame(out)
    E = E[E.dMF.abs() <= 20]
    E.to_csv("research/inst_accum_events.csv", index=False)
    print(f"events in point-in-time top-300: {len(E)}; cohorts: {E.q.nunique()}; symbols: {E.Symbol.nunique()}")

    def quint(col, h):
        d = E.dropna(subset=[col, f"x{h}"]).copy()
        d = d[d.groupby("q")[col].transform("size") >= 25]  # pre-registered minimum cohort size
        d["Qn"] = d.groupby("q")[col].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) + 1)
        c = d.groupby(["q", "Qn"])[f"x{h}"].mean().unstack()
        return c, d

    def report(col, h, primary=False):
        c, d = quint(col, h)
        q5 = c[5] - COST
        spread = c[5] - c[1]
        n = len(q5)
        t = q5.mean() / (q5.std(ddof=1) / np.sqrt(n)) if n > 2 else np.nan
        half = n // 2
        h1, h2 = q5.iloc[:half].mean(), q5.iloc[half:].mean()
        pos = (q5 > 0).mean()
        print(f"\n[{col} quintiles, h={h}] cohorts {n}")
        print("  mean excess by quintile (%): " + "  ".join(f"Q{k} {c[k].mean()*100:+.2f}" for k in range(1, 6)))
        print(f"  Q5 net {q5.mean()*100:+.2f}%  t={t:.2f}  halves {h1*100:+.2f}/{h2*100:+.2f}  "
              f"cohorts positive {pos*100:.0f}%  Q5-Q1 {spread.mean()*100:+.2f}%")
        yr = q5.groupby(q5.index.year).mean() * 100
        print("  Q5 net by year:", {k: round(v, 2) for k, v in yr.items()})
        if primary:
            checks = {"mean>0": q5.mean() > 0, "t>=2.5": t >= 2.5, "both halves>0": h1 > 0 and h2 > 0,
                      ">=60% cohorts": pos >= 0.6, "spread>0": spread.mean() > 0}
            print("  PRIMARY CHECKS:", checks, "=>", "PASS" if all(checks.values()) else "FAIL")

    report("dMF", H_PRIMARY, primary=True)
    report("dMF", H_SECONDARY)
    report("dFPI", H_PRIMARY)
    report("dFPI", H_SECONDARY)
    for h in (H_SECONDARY, H_PRIMARY):
        a = E[(E.dMF >= 1.0)].dropna(subset=[f"x{h}"])
        g = a.groupby("q")[f"x{h}"].mean() - COST
        print(f"\n[dMF >= +1.0pt, h={h}] events {len(a)}, cohorts {len(g)}: mean net {g.mean()*100:+.2f}%, "
              f"cohorts positive {(g > 0).mean()*100:.0f}%")


if __name__ == "__main__":
    main()

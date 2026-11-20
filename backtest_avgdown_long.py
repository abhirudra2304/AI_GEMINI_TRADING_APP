"""Long walk-forward check (2020-2026) of the 2026-11-13 AVGDOWN_STRICT volume/tail rule.
Cache-only: NSE bhavcopy history in delivery_cache/, split/bonus-adjusted by strategy_paper.load().

PRE-REGISTERED (written before any result was seen; supersedes the 2026-11 short test, n=31):
  Universe: point-in-time liquid set = top-300 by 60-session median turnover as of the signal day
            (NOT config.Universe, which has hindsight bias).
  Signal day i: support test. Supports = SMA50, SMA100, SMA200, 52-week low (volume-node supports are
            omitted here: not vectorisable over 6 years; applied identically to every group, so the
            comparison is fair but this is a simplified rule).
  Groups: NEW_STRICT, OLD_STRICT, REMOVED (old STRICT dropped by new rule), HVST, BASE
          (support touched, close > 100DMA and 200DMA, any volume/tail).
  Entry = next open (adjusted). Returns 5/10/20 sessions after entry.
  PRIMARY: date-clustered, paired excess of NEW_STRICT over BASE, 10-day return.
  PASS only if ALL: n(NEW_STRICT) >= 300; mean excess > 0; clustered t >= 2; excess > 0 in both
          halves of the period AND in >= 4 of the 6 calendar years (2021-2026);
          and NEW_STRICT mean 10d return net of 0.25% round-trip cost > 0.
  SECONDARY: REMOVED and HVST should have LOWER mean 10d return than BASE if the WAIT flag is right.
"""
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_paper as sp

HZ = (5, 10, 20)
COST = 0.0025


def build():
    data = sp.load(n_files=2000, since=date(2020, 1, 1))
    C, CA, TO = data["C"], data["CA"], data["TO"]
    fr = []
    for f in sorted(x for x in os.listdir(sp.CACHE) if x.endswith(".parquet")):
        d = pd.read_parquet(os.path.join(sp.CACHE, f), columns=["SYMBOL", "SERIES", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "TTL_TRD_QNTY"])
        d = d[d.SERIES == "EQ"]
        d["D"] = pd.Timestamp(f[:8])
        fr.append(d)
    X = pd.concat(fr)
    pv = lambda c: X.pivot_table(index="D", columns="SYMBOL", values=c).reindex(index=C.index, columns=C.columns)
    O, H, L, V = pv("OPEN_PRICE"), pv("HIGH_PRICE"), pv("LOW_PRICE"), pv("TTL_TRD_QNTY")
    f = (CA / C).replace([np.inf, -np.inf], np.nan)
    return CA, O * f, H * f, L * f, V / f, TO


def signals(c, o, h, l, v, to):
    s50, s100, s200 = (c.rolling(n, min_periods=n).mean() for n in (50, 100, 200))
    low52 = l.rolling(250, min_periods=250).min()
    prev = c.shift(1)
    hit = pd.DataFrame(False, index=c.index, columns=c.columns)
    for lv in (s50, s100, s200, low52):
        hit |= (prev > lv) & (l <= lv * 1.01) & (c >= lv * 0.995)
    span = (h - l).where((h - l) > 0)
    rng = ((c - l) / span).fillna(0.5)
    tail = ((np.minimum(o, c) - l) / span).fillna(0.0)
    volx = v / v.shift(1).rolling(15).mean()
    down = c < prev
    above = (c > s100) & (c > s200)
    vol_ok = (volx < 1.0) | (tail > 0.5)
    new = hit & above & (rng >= 0.35) & np.where(down, vol_ok, volx >= 1.0)
    old = hit & above & (rng >= 0.35) & (volx >= 1.0)
    hvst = hit & down & (volx > 1.5) & ~(tail > 0.5) & ~new
    base = hit & above
    U = to.rolling(60, min_periods=40).median().rank(axis=1, ascending=False) <= 300
    return {"NEW_STRICT": new & U, "OLD_STRICT": old & U, "REMOVED": old & ~new & U, "HVST": hvst & U, "BASE": base & U}


def run():
    c, o, h, l, v, to = build()
    g = signals(c, o, h, l, v, to)
    entry = o.shift(-1)
    rows = []
    for name, m in g.items():
        for hz in HZ:
            r = c.shift(-(hz + 1)) / entry - 1  # close hz sessions after the entry day
            s = r.where(m).stack().dropna().rename(f"r{hz}")
            if hz == HZ[0]:
                cur = s.to_frame()
            else:
                cur = cur.join(s, how="outer")
        cur["group"] = name
        rows.append(cur.reset_index())
    df = pd.concat(rows)
    df.columns = ["date", "sym"] + [f"r{h}" for h in HZ] + ["group"]
    df.to_csv("backtest_avgdown_long_signals.csv", index=False)
    return df


def report(df):
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["r10"])
    print(f"signals {len(df)}  period {df.date.min().date()} to {df.date.max().date()}")
    for hz in HZ:
        print(f"\n-- {hz}-day forward return from next open --")
        for g in ("BASE", "OLD_STRICT", "NEW_STRICT", "REMOVED", "HVST"):
            x = df[df.group == g][f"r{hz}"].dropna()
            print(f"{g:11} n={len(x):6d} mean={x.mean()*100:+.2f}% win={(x>0).mean()*100:.0f}%")
    nw, bs = df[df.group == "NEW_STRICT"], df[df.group == "BASE"]

    def clustered(a, b):
        da, db = a.groupby("date").r10.mean(), b.groupby("date").r10.mean()
        j = pd.concat([da, db], axis=1, keys=["a", "b"]).dropna()
        e = j.a - j.b
        return len(j), e.mean() * 100, e.mean() / (e.std() / np.sqrt(len(e))) if len(e) > 2 and e.std() > 0 else np.nan

    nd, ex, t = clustered(nw, bs)
    mid = df.date.sort_values().iloc[len(df) // 2]
    halves = []
    for nm, sub in (("first half", df[df.date <= mid]), ("second half", df[df.date > mid])):
        n2, e2, t2 = clustered(sub[sub.group == "NEW_STRICT"], sub[sub.group == "BASE"])
        halves.append(e2)
        print(f"{nm}: dates={n2} excess10d={e2:+.2f}% t={t2:.2f}")
    yrs = {}
    for y, sub in df.groupby(df.date.dt.year):
        n2, e2, t2 = clustered(sub[sub.group == "NEW_STRICT"], sub[sub.group == "BASE"])
        yrs[y] = e2
        print(f"  {y}: NEW n={int((sub.group=='NEW_STRICT').sum())} dates={n2} excess10d={e2:+.2f}%")
    pos_years = sum(1 for y, e in yrs.items() if e > 0 and y >= 2021)
    net = nw.r10.mean() * 100 - COST * 100
    ok = len(nw) >= 300 and ex > 0 and t >= 2 and all(e > 0 for e in halves) and pos_years >= 4 and net > 0
    print(f"\nPRIMARY: NEW_STRICT n={len(nw)} dates={nd} excess10d={ex:+.2f}% clustered t={t:.2f} "
          f"halves {halves[0]:+.2f}/{halves[1]:+.2f} positive years(2021+)={pos_years} net-of-cost mean10d={net:+.2f}% -> {'PASS' if ok else 'FAIL'}")
    b = bs.r10.mean() * 100
    for gname in ("REMOVED", "HVST"):
        x = df[df.group == gname].r10
        print(f"{gname}: n={len(x)} mean10d={x.mean()*100:+.2f}% vs BASE {b:+.2f}% ({'lower: flag right' if x.mean()*100 < b else 'NOT lower: flag not supported'})")


if __name__ == "__main__":
    report(pd.read_csv("backtest_avgdown_long_signals.csv") if "--report" in sys.argv else run())

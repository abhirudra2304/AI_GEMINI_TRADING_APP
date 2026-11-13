"""Walk-forward check of the 2026-11-13 AVGDOWN_STRICT volume/tail rule (cache-only, no API).

PRE-REGISTERED (written before any result was seen):
  Signal day i = every (symbol, day) in the universe with >=250 prior sessions. Entry = open of day i+1.
  Groups: NEW_STRICT (new rule), OLD_STRICT (old rule), REMOVED (old STRICT that the new rule dropped),
          HVST (HIGH_VOL_SUPPORT_TEST), BASE (support touched, close>100DMA&200DMA, any volume/tail).
  Primary test: NEW_STRICT mean 10-day return minus BASE mean 10-day return (same universe, same period).
  PASS only if ALL: n(NEW_STRICT) >= 100; excess > 0; excess > 0 in BOTH halves of the period;
  and the signal-level t-stat of NEW_STRICT-minus-BASE >= 2.
  Secondary (informational): REMOVED and HVST should do WORSE than BASE if the WAIT flag is right.
Caveats stated up front: ~150 evaluable sessions only (400-day caches), universe has hindsight bias
(config universe), overlapping signals inflate the t-stat, no costs applied (relative comparison).
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import avgdown_watch as aw
import config

OUT = "backtest_avgdown_rule_signals.csv"
HZ = (5, 10, 20)


def run():
    rows = []
    syms = sorted(config.Universe.TARGET_UNIVERSE)
    for k, s in enumerate(syms):
        try:
            d = aw.daily(s)
        except Exception:
            continue
        d = d[~d.index.duplicated()].sort_index()
        if len(d) < 300:
            continue
        o, c = d["open"].values, d["close"].values
        for i in range(250, len(d) - 21):
            w = d.iloc[: i + 1]
            try:
                sig, x = aw.classify(w)
            except Exception:
                continue
            hit = bool(x["hit"])
            if not hit:
                continue
            t = x["t"]
            above = t.close > x["s100"] and t.close > x["s200"]
            base_old = x["volx"] >= 1.0 and x["rng"] >= 0.35
            old_strict = base_old and above
            entry = o[i + 1]
            if not entry or np.isnan(entry):
                continue
            r = {"sym": s, "date": d.index[i], "new": sig == "STRICT", "old": old_strict,
                 "hvst": sig == "HIGH_VOL_SUPPORT_TEST", "base": above}
            for h in HZ:
                r[f"r{h}"] = c[i + h] / entry - 1
            rows.append(r)
        if k % 40 == 0:
            print(k, len(rows), flush=True)
    pd.DataFrame(rows).to_csv(OUT, index=False)


def stats(x, h):
    v = x[f"r{h}"].dropna()
    return len(v), v.mean() * 100, (v > 0).mean() * 100 if len(v) else np.nan


def welch(a, b):
    if len(a) < 5 or len(b) < 5:
        return np.nan
    return (a.mean() - b.mean()) / np.sqrt(a.var() / len(a) + b.var() / len(b))


def report():
    df = pd.read_csv(OUT, parse_dates=["date"])
    df["removed"] = df.old & ~df.new
    mid = df.date.sort_values().iloc[len(df) // 2]
    print(f"signal-days with a support touch: {len(df)}  period {df.date.min().date()} to {df.date.max().date()}  split {mid.date()}")
    groups = {"BASE": df[df.base], "OLD_STRICT": df[df.old], "NEW_STRICT": df[df.new], "REMOVED": df[df.removed], "HVST": df[df.hvst]}
    for h in HZ:
        print(f"\n-- {h}-day forward return from next open --")
        for g, x in groups.items():
            n, m, w = stats(x, h)
            print(f"{g:11} n={n:5d} mean={m:+.2f}% win={w:.0f}%")
    h = 10
    b, n_ = groups["BASE"][f"r{h}"], groups["NEW_STRICT"][f"r{h}"]
    ex = (n_.mean() - b.mean()) * 100
    t = welch(n_, b)
    ex1 = ex2 = np.nan
    for nm, sub in (("first half", df[df.date <= mid]), ("second half", df[df.date > mid])):
        bb, nn = sub[sub.base][f"r{h}"], sub[sub.new][f"r{h}"]
        e = (nn.mean() - bb.mean()) * 100 if len(nn) else np.nan
        print(f"{nm}: NEW n={len(nn)} base n={len(bb)} excess10d={e:+.2f}%")
        if nm == "first half": ex1 = e
        else: ex2 = e
    ok = len(n_) >= 100 and ex > 0 and ex1 > 0 and ex2 > 0 and t >= 2
    print(f"\nPRIMARY: NEW_STRICT n={len(n_)} excess10d={ex:+.2f}% t={t:.2f} halves {ex1:+.2f}/{ex2:+.2f} -> {'PASS' if ok else 'FAIL'}")
    for g in ("REMOVED", "HVST"):
        x = groups[g][f"r{h}"]
        print(f"{g}: n={len(x)} mean10d={x.mean()*100:+.2f}% vs BASE {b.mean()*100:+.2f}% (WAIT flag right if lower)")


if __name__ == "__main__":
    if "--report" not in sys.argv:
        run()
    report()

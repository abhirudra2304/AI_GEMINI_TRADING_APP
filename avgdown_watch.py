"""Average-down watch for held names: support levels + reversal test. Read-only, no scanner changes.

    python avgdown_watch.py                      # settled data from historical_data/ caches
    python avgdown_watch.py --live               # also pull today's bar from Angel (5 calls)
    python avgdown_watch.py HAL BDL --live

Support = 50/100/200DMA, 52w low and the heaviest-volume price bins of the last 250 sessions that sit
at or below the latest close. Signals on the latest bar:
  STRICT = touched a support (low within 1% of it) AND close held above it AND volume >= 1.0x the
           prior 15-day average AND close in the upper 65% of the day's range AND close above the
           100 and 200DMA (the 2026-10-08 average-down bar).
  EARLY  = same touch/hold/volume/range test, but the 100/200DMA condition is waived and the close
           must be above the prior close (first sign of a base on a broken name).
Intraday (--live) volume is partial-day, so the volume test is understated until after 15:35.
"""
import argparse
import os

import numpy as np
import pandas as pd

HOLD = {"SOLARINDS": 18769.0, "DIXON": 14462.0}
DEFAULT = ["SOLARINDS", "NETWEB", "DIXON", "HAL", "BDL"]


def _load(f):
    d = pd.read_parquet(os.path.join("historical_data", f))
    d.columns = [c.lower() for c in d.columns]
    if "timestamp" in d.columns:
        d = d.set_index("timestamp")
    d.index = pd.to_datetime(d.index).tz_localize(None)
    return d


def daily(sym):
    fs = [f for f in os.listdir("historical_data") if f.startswith(sym + "_ONE_DAY") and f.endswith(".parquet")]
    return max((_load(f) for f in fs), key=lambda x: (x.index.max(), len(x)))


def live_bar(sym, d):
    """Merge today's Angel bar onto the cached history (a fresh 250-day fetch alone would shorten
    the 200DMA and 52w-low windows to ~170 sessions)."""
    from data_broker import DataBroker
    b = getattr(live_bar, "_b", None) or DataBroker()
    live_bar._b = b
    n = b.fetch_daily_candles(sym, days_back=10)
    n.columns = [c.lower() for c in n.columns]
    if "timestamp" in n.columns:
        n = n.set_index("timestamp")
    n.index = pd.to_datetime(n.index).tz_localize(None).normalize()
    d = d.copy()
    d.index = d.index.normalize()
    return pd.concat([d[~d.index.isin(n.index)], n[d.columns.intersection(n.columns)]]).sort_index()


def supports(d):
    c = d["close"]
    last = c.iloc[-1]
    lv = {"SMA50": c.tail(50).mean(), "SMA100": c.tail(100).mean(), "SMA200": c.tail(200).mean(),
          "52wLow": d["low"].tail(250).min()}
    t = d.tail(250)
    bins = pd.cut(t["close"], 40)
    vol = t.groupby(bins, observed=True)["volume"].sum().sort_values(ascending=False)
    for iv in vol.index[:6]:
        lv[f"VolNode {iv.mid:.0f}"] = iv.mid
    return {k: v for k, v in lv.items() if v <= last * 1.01}


def evaluate(sym, d):
    """Signal dict for the latest bar of `d` (used by journal.py; check() prints the same)."""
    t, p = d.iloc[-1], d.iloc[-2]
    c = d["close"]
    rng = (t.close - t.low) / (t.high - t.low) if t.high > t.low else 0.5
    volx = t.volume / d["volume"].iloc[-16:-1].mean()
    sup = supports(d)
    # a support TEST: previous close above the level, then dipped to it and held (2026-11-06:
    # without the p.close condition a stock breaking UP through a level from below counted)
    hit = [(k, v) for k, v in sup.items() if p.close > v and t.low <= v * 1.01 and t.close >= v * 0.995]
    s100, s200 = c.tail(100).mean(), c.tail(200).mean()
    base = bool(hit) and volx >= 1.0 and rng >= 0.35
    strict = base and t.close > s100 and t.close > s200
    early = base and t.close > p.close
    below = sorted((v for v in sup.values() if v < t.close), reverse=True)
    return {"date": d.index[-1].date(), "close": float(t.close), "signal": "STRICT" if strict else "EARLY" if early else "",
            "touched": ";".join(f"{k} {v:.0f}" for k, v in hit), "rng": round(rng, 2), "volx": round(volx, 2),
            "near_support_pct": round(100 * (below[0] / t.close - 1), 1) if below else None}


def check(sym, live):
    d = daily(sym)
    if live:
        d = live_bar(sym, d)
    t, p = d.iloc[-1], d.iloc[-2]
    c = d["close"]
    rng = (t.close - t.low) / (t.high - t.low) if t.high > t.low else 0.5
    volx = t.volume / d["volume"].iloc[-16:-1].mean()
    sup = supports(d)
    # a support TEST: previous close above the level, then dipped to it and held (2026-11-06:
    # without the p.close condition a stock breaking UP through a level from below counted)
    hit = [(k, v) for k, v in sup.items() if p.close > v and t.low <= v * 1.01 and t.close >= v * 0.995]
    s100, s200 = c.tail(100).mean(), c.tail(200).mean()
    base = bool(hit) and volx >= 1.0 and rng >= 0.35
    strict = base and t.close > s100 and t.close > s200
    early = base and t.close > p.close
    sig = "STRICT" if strict else "EARLY" if early else "-"
    near = sorted(sup.items(), key=lambda kv: -kv[1])[:3]
    pnl = f"{100 * (t.close / HOLD[sym] - 1):+.1f}% vs entry {HOLD[sym]:.0f}" if sym in HOLD else ""
    print(f"{sym:10} {d.index[-1].date()} close {t.close:9.1f} chg {100 * (t.close / p.close - 1):+5.2f}% "
          f"rng {rng:.2f} vol {volx:.2f}x  signal {sig:6} {pnl}")
    print("           supports below: " + ", ".join(f"{k} {v:.0f}" for k, v in near)
          + f" | 100DMA {s100:.0f} 200DMA {s200:.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=DEFAULT)
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    for s in a.symbols:
        try:
            check(s, a.live)
        except Exception as e:
            print(f"{s}: ERR {e}")

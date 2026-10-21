"""Pullback-to-Confluence (P2C) checklist for ADDING to long-term holdings.

Not a trade signal and not wired into any scan: the pullback/bounce family failed
every point-in-time test here (bounce rule -0.09R 2021-26, short-term bounce -0.22R).
Its job is narrower - stop averaging down into a broken trend, and wait for a
controlled dip into support that has started to turn. Cache-only, no API calls.

    python p2c_check.py                 # the ten holdings
    python p2c_check.py NETWEB PTCIL    # any cached symbols

Checks (each PASS / FAIL / n/a):
 1 TREND     close > 200-DMA and 200-DMA higher than 20 sessions ago
 2 DEPTH     pullback retraces 30-62% of the last up-leg (swing low -> 120d high)
 3 ZONE      >= 2 supports within 2.5% of close (20/50/100/200-DMA, Fib 38.2/50/61.8)
 4 VOLUME    last-5-day volume <= 50-day average and no >2x down-day in 10 sessions
 5 RSI       RSI(14) between 35 and 55 (cooled, not broken)
 6 CANDLE    last daily bar is a hammer or bullish engulfing
 7 MSS       15-min: after the 2-day low, a close above the last swing high before it
ADD-READY = 1-5 all PASS and (6 or 7 PASS). TREND FAIL means do not average down.
"""
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

HOLDINGS = ["DATAPATTNS", "DIXON", "SOLARINDS", "TCS", "BEL", "BDL", "HAL", "KAYNES", "CONCOR", "GODFRYPHLP"]
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historical_data")


def _load(sym, tf):
    p = os.path.join(_DIR, f"{sym}_{tf}.parquet")
    if not os.path.exists(p):
        return None, None
    d = pd.read_parquet(p)
    if "Timestamp" in d.columns:
        d = d.set_index("Timestamp")
    return d.sort_index(), datetime.fromtimestamp(os.path.getmtime(p))


def _rsi(c, n=14):
    x = c.diff()
    g = x.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l = (-x.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / l)


def _mss(m15):
    """True if, after the lowest low of the last 2 sessions, a 15-min close cleared
    the highest high of the 10 bars before that low (a lower-timeframe higher high)."""
    if m15 is None or len(m15) < 40:
        return None
    days = sorted(set(m15.index.date))[-2:]
    w = m15[np.isin(m15.index.date, days)]
    if len(w) < 5:
        return None
    lo_pos = m15.index.get_loc(w["Low"].idxmin())
    before = m15.iloc[max(0, lo_pos - 10):lo_pos]
    after = m15.iloc[lo_pos + 1:]
    if before.empty or after.empty:
        return False
    return bool((after["Close"] > before["High"].max()).any())


def check(sym):
    d, mt = _load(sym, "ONE_DAY_400")
    if d is None or len(d) < 220:
        return {"Symbol": sym, "Verdict": "NO DATA"}
    m15, _ = _load(sym, "FIFTEEN_MINUTE_80")
    c, h, l, o, v = d["Close"], d["High"], d["Low"], d["Open"], d["Volume"]
    px = c.iloc[-1]
    sma = {n: c.rolling(n).mean() for n in (20, 50, 100, 200)}
    r = {"Symbol": sym, "Close": round(px, 2), "Bar": d.index[-1].strftime("%m-%d"),
         "Settled": mt.hour * 60 + mt.minute >= 15 * 60 + 35 or mt.date() > d.index[-1].date()}

    r["TREND"] = px > sma[200].iloc[-1] and sma[200].iloc[-1] > sma[200].iloc[-21]

    win = d.iloc[-120:]
    hi_i = win["High"].idxmax()
    hi = win["High"].max()
    pre = d.loc[:hi_i].iloc[-120:]
    lo = pre["Low"].min()
    leg = hi - lo
    retr = (hi - px) / leg * 100 if leg > 0 else np.nan
    r["Retr%"] = round(retr, 1)
    r["Off_Hi%"] = round((px / hi - 1) * 100, 1)
    r["DEPTH"] = 30 <= retr <= 62

    levels = {f"{n}DMA": sma[n].iloc[-1] for n in (20, 50, 100, 200)}
    levels.update({f"Fib{f}": hi - leg * f / 100 for f in (38.2, 50, 61.8)})
    near = [k for k, lv in levels.items() if abs(px / lv - 1) <= 0.025]
    r["Supports"] = ",".join(near) or "-"
    r["ZONE"] = len(near) >= 2

    vol50 = v.iloc[-55:-5].mean()
    dn = (c.diff() < 0) & (v > 2 * v.rolling(50).mean().shift())
    r["Vol5/50"] = round(v.iloc[-5:].mean() / vol50, 2) if vol50 else np.nan
    r["VOLUME"] = bool(r["Vol5/50"] <= 1.0 and not dn.iloc[-10:].any())

    rsi = _rsi(c).iloc[-1]
    r["RSI"] = round(rsi, 1)
    r["RSI_OK"] = 35 <= rsi <= 55

    body = abs(c.iloc[-1] - o.iloc[-1])
    rng = h.iloc[-1] - l.iloc[-1]
    lower_wick = min(c.iloc[-1], o.iloc[-1]) - l.iloc[-1]
    upper_wick = h.iloc[-1] - max(c.iloc[-1], o.iloc[-1])
    hammer = rng > 0 and lower_wick >= 2 * body and upper_wick <= body and c.iloc[-1] >= l.iloc[-1] + 0.6 * rng
    engulf = (c.iloc[-2] < o.iloc[-2] and c.iloc[-1] > o.iloc[-1]
              and c.iloc[-1] >= o.iloc[-2] and o.iloc[-1] <= c.iloc[-2])
    r["CANDLE"] = bool(hammer or engulf)
    r["MSS"] = _mss(m15)

    core = all(r[k] for k in ("TREND", "DEPTH", "ZONE", "VOLUME", "RSI_OK"))
    trigger = r["CANDLE"] or bool(r["MSS"])
    if not r["TREND"]:
        r["Verdict"] = "NO - trend broken"
    elif core and trigger:
        r["Verdict"] = "ADD-READY"
    elif core:
        r["Verdict"] = "IN ZONE - wait for turn"
    else:
        r["Verdict"] = "WAIT"
    return r


def main(symbols):
    rows = [check(s) for s in symbols]
    df = pd.DataFrame(rows)
    fmt = lambda x: "PASS" if x is True else ("FAIL" if x is False else ("n/a" if x is None else x))
    for k in ("TREND", "DEPTH", "ZONE", "VOLUME", "RSI_OK", "CANDLE", "MSS"):
        if k in df.columns:
            df[k] = df[k].map(fmt)
    cols = ["Symbol", "Bar", "Close", "Off_Hi%", "Retr%", "RSI", "Vol5/50", "TREND", "DEPTH", "ZONE",
            "VOLUME", "RSI_OK", "CANDLE", "MSS", "Verdict"]
    pd.set_option("display.width", 250)
    print(df[[c for c in cols if c in df.columns]].to_string(index=False))
    print("\nSupports within 2.5%:")
    for _, r in df.iterrows():
        print(f"  {r['Symbol']:<11} {r.get('Supports', '-')}")
    if "Settled" in df.columns and not df["Settled"].fillna(True).all():
        print("\nWARNING: some daily bars were cached before 15:35 - last close may be provisional:",
              ", ".join(df.loc[~df["Settled"].fillna(True), "Symbol"]))
    print("\nChecklist for adding to long-term holdings - not a trade signal (pullback rules showed no edge in testing).")


if __name__ == "__main__":
    main([s.upper() for s in sys.argv[1:]] or HOLDINGS)

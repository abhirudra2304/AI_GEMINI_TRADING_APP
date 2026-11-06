"""Nightly journal: one row per trading day + every watch signal as an event, so a month later
`journal_review.py` can show exactly what happened. Read-only towards the scanners; no broker calls.

    python journal.py            # journal the latest settled session in delivery_cache/
    python journal.py --force    # rewrite that day's rows

Runs in run_nightly.ps1 after apply_official_closes.py, score_new_outcomes.py and fii_dii_log.py.
Writes (append, de-duplicated by date):
  journal/daily.csv     market, breadth, flows, system health, brief size
  journal/holdings.csv  per held / watched symbol: close, change, delivery, average-down signal
  journal/events.csv    signals to score later: AVGDOWN_STRICT/EARLY, DELIV_ACCUMULATION/DISTRIBUTION,
                        INST_DEAL_BUY/SELL (one-way bulk/block deals by institution-like clients)
"""
import argparse
import glob
import io
import os
import re
from datetime import datetime

import numpy as np
import pandas as pd
import requests

import avgdown_watch as aw
import config
from flow_watch import INST_HINTS

OUT = "journal"
HOLDINGS = ["DATAPATTNS", "DIXON", "SOLARINDS", "TCS", "BEL", "BDL", "HAL", "KAYNES", "CONCOR", "GODFRYPHLP", "NETWEB"]
WATCH = ["ICICIBANK", "HDFCBANK", "KOTAKBANK", "AXISBANK", "RELIANCE", "SHRIRAMFIN", "MUTHOOTFIN"]
INDICES = {"Nifty 50": "nifty", "Nifty 500": "n500", "NIFTY Midcap 100": "mid100", "NIFTY Smallcap 100": "small100",
           "Nifty Bank": "bank", "Nifty IT": "it", "Nifty India Defence": "defence", "India VIX": "vix"}
UA = {"User-Agent": "Mozilla/5.0"}


def _append(name, df, day, force):
    p = os.path.join(OUT, name)
    old = pd.read_csv(p) if os.path.exists(p) else pd.DataFrame()
    if len(old) and "date" in old:
        if not force and (old["date"].astype(str) == str(day)).any():
            return 0
        old = old[old["date"].astype(str) != str(day)]
    pd.concat([old, df], ignore_index=True).to_csv(p, index=False)
    return len(df)


def _bhav(n=60):
    fs = sorted(glob.glob("delivery_cache/2*.parquet"))[-n:]
    X = pd.concat([pd.read_parquet(f).assign(D=os.path.basename(f)[:8]) for f in fs])
    return X[X.SERIES == "EQ"]


def _indices(day):
    try:
        r = requests.get(f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{day:%d%m%Y}.csv",
                         headers=UA, timeout=20)
        x = pd.read_csv(io.StringIO(r.text))
        x.columns = [c.strip() for c in x.columns]
        x = x.set_index("Index Name")
        out = {}
        for name, key in INDICES.items():
            if name in x.index:
                out[key] = float(x.loc[name, "Closing Index Value"])
                out[key + "_pct"] = float(x.loc[name, "Change(%)"])
        return out
    except Exception:
        return {}


def _health(day):
    """Decision-run timing and data-staleness from that day's logs."""
    h = {}
    p = f"logs/decision_{day:%Y-%m-%d}.log"
    if os.path.exists(p):
        txt = open(p, encoding="utf-8", errors="ignore").read()
        m = re.search(r"^(\d\d:\d\d:\d\d) brief", txt, re.M)
        h["brief_time"] = m.group(1) if m else ""
        h["engine_snapshot_ran"] = "engine snapshot skipped" not in txt
        h["stale_served"] = txt.count("serving stale")
    b = f"logs/brief_{day:%Y-%m-%d}.log"
    if os.path.exists(b):
        txt = open(b, encoding="utf-8", errors="ignore").read()
        for k in ("BTST", "SWING", "GRIND"):
            m = re.search(rf"{k} CANDIDATES \((\d+)\)", txt)
            h[f"brief_{k.lower()}_n"] = int(m.group(1)) if m else None
        m = re.search(r"BTST source\s*:\s*(.+)", txt)
        h["brief_source"] = m.group(1).strip()[:40] if m else ""
    return h


def _deals(day, universe):
    rows = []
    for kind in ("bulk", "block"):
        try:
            r = requests.get(f"https://nsearchives.nseindia.com/content/equities/{kind}.csv", headers=UA, timeout=20)
            if r.status_code != 200 or "NO RECORDS" in r.text[:200]:
                continue
            d = pd.read_csv(io.StringIO(r.text))
            d.columns = [c.strip() for c in d.columns]
            d["kind"] = kind
            rows.append(d)
        except Exception:
            pass
    if not rows:
        return []
    d = pd.concat(rows, ignore_index=True)
    d["Date"] = pd.to_datetime(d["Date"].astype(str).str.strip(), format="%d-%b-%Y", errors="coerce")
    d = d[d["Date"].dt.date == day]
    d["Symbol"] = d["Symbol"].astype(str).str.strip()
    d = d[d.Symbol.isin(universe)]
    d["qty"] = pd.to_numeric(d["Quantity Traded"].astype(str).str.replace(",", ""), errors="coerce")
    d["px"] = pd.to_numeric(d["Trade Price / Wght. Avg. Price"].astype(str).str.replace(",", ""), errors="coerce")
    d["sg"] = np.where(d["Buy/Sell"].astype(str).str.upper().str.startswith("B"), 1, -1)
    ev = []
    for (sym, cl), g in d.groupby(["Symbol", "Client Name"]):
        gross = (g.qty * g.px).sum() / 1e7
        net = (g.qty * g.px * g.sg).sum() / 1e7
        inst = any(h in str(cl).upper() for h in INST_HINTS)
        if inst and gross >= 10 and abs(net) / gross >= 0.8:   # one-way, >= Rs 10 cr, institution-like client
            ev.append({"date": day, "symbol": sym, "type": "INST_DEAL_BUY" if net > 0 else "INST_DEAL_SELL",
                       "price": round(g.px.mean(), 2), "detail": f"{cl} {kind_of(g)} net Rs {net:+.0f} cr"})
    return ev


def kind_of(g):
    return "/".join(sorted(g["kind"].unique()))


def main(force=False):
    os.makedirs(OUT, exist_ok=True)
    X = _bhav()
    day = datetime.strptime(X.D.max(), "%Y%m%d").date()
    T = X[X.D == X.D.max()]
    U = list(config.Universe.TARGET_UNIVERSE)

    # ---- daily row
    tu = T[T.SYMBOL.isin(U)]
    chg = tu.CLOSE_PRICE / tu.PREV_CLOSE - 1
    C = X.pivot_table(index="D", columns="SYMBOL", values="CLOSE_PRICE")[[s for s in U if s in set(X.SYMBOL)]]
    above50 = (C.iloc[-1] > C.tail(50).mean()).mean() * 100
    fii = pd.read_csv("fii_dii_history.csv") if os.path.exists("fii_dii_history.csv") else pd.DataFrame()
    f = fii[fii.date == str(day)] if len(fii) else fii
    row = {"date": day, **_indices(day),
           "univ_adv": int((chg > 0).sum()), "univ_dec": int((chg < 0).sum()),
           "univ_pct_above_50dma": round(above50, 1),
           "mkt_adv": int((T.CLOSE_PRICE > T.PREV_CLOSE).sum()), "mkt_dec": int((T.CLOSE_PRICE < T.PREV_CLOSE).sum()),
           "fii_net": float(f[f.category.str.startswith("FII")].net.sum()) if len(f) else None,
           "dii_net": float(f[f.category == "DII"].net.sum()) if len(f) else None,
           **_health(day)}
    n_daily = _append("daily.csv", pd.DataFrame([row]), day, force)

    # ---- holdings / watch rows + events
    hold_rows, events = [], []
    for sym in HOLDINGS + WATCH:
        g = X[X.SYMBOL == sym].sort_values("D")
        r = {"date": day, "symbol": sym, "group": "HOLD" if sym in HOLDINGS else "WATCH"}
        if len(g) >= 25 and g.D.iloc[-1] == X.D.max():
            t, base = g.iloc[-1], g.iloc[-21:-1]
            qx = t.DELIV_QTY / base.DELIV_QTY.mean()
            dp = t.DELIV_PER - base.DELIV_PER.mean()
            spike = ("ACCUMULATION" if t.CLOSE_PRICE >= t.PREV_CLOSE else "DISTRIBUTION") if qx >= 1.5 and dp >= 5 else ""
            r.update(close=t.CLOSE_PRICE, chg_pct=round(100 * (t.CLOSE_PRICE / t.PREV_CLOSE - 1), 2),
                     deliv_pct=t.DELIV_PER, deliv_x=round(qx, 2), deliv_spike=spike)
            if spike:
                events.append({"date": day, "symbol": sym, "type": f"DELIV_{spike}", "price": t.CLOSE_PRICE,
                               "detail": f"delivery {t.DELIV_PER:.0f}% ({dp:+.0f} pts), {qx:.1f}x qty"})
        try:
            d = aw.daily(sym)
            if d.index[-1].date() == day:
                a = aw.evaluate(sym, d)
                r.update(avgdown=a["signal"], near_support_pct=a["near_support_pct"], vol_x=a["volx"])
                if a["signal"]:
                    events.append({"date": day, "symbol": sym, "type": f"AVGDOWN_{a['signal']}", "price": a["close"],
                                   "detail": f"touched {a['touched']}; vol {a['volx']}x, range {a['rng']}"})
            else:
                r["avgdown"] = "STALE_CACHE"
        except Exception as e:
            r["avgdown"] = f"ERR {type(e).__name__}"
        hold_rows.append(r)
    n_hold = _append("holdings.csv", pd.DataFrame(hold_rows), day, force)

    events += _deals(day, set(U) | set(HOLDINGS))
    n_ev = _append("events.csv", pd.DataFrame(events, columns=["date", "symbol", "type", "price", "detail"]), day, force) \
        if events else 0
    print(f"journal {day}: daily {n_daily}, holdings {n_hold}, events {n_ev}"
          + (" (already journaled; --force to rewrite)" if not n_daily and not force else ""))
    for e in events:
        print(f"  {e['type']:22} {e['symbol']:11} {e['detail']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    main(ap.parse_args().force)

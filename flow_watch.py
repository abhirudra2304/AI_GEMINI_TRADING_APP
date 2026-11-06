"""Delivery-spike and institutional-deal watch for named stocks. Read-only; no broker API, no scanner changes.

    python flow_watch.py                       # NETWEB SOLARINDS
    python flow_watch.py HAL BDL

Run after the nightly job (it extends delivery_cache/ at ~20:15 and publishes the day's bhavcopy).
  1. DELIVERY: latest session's delivered quantity vs the mean of the prior 20 sessions, and delivery %
     vs its 20-session mean. SPIKE = qty >= 1.5x AND delivery % >= mean + 5 pts. ACCUMULATION when the
     close is above the previous close, DISTRIBUTION when below. Delivery data cannot say WHO bought.
  2. DEALS: NSE bulk.csv / block.csv are published for the latest session only; matching rows are
     printed and appended to flow_watch_deals.csv so history builds up (client names are shown as filed).
  3. HOLDINGS: latest quarterly MF / DII / FPI change from the newest fundamentals/ table. Per-stock
     FII/DII flows are NOT published intraday or daily - only these deals and the quarterly filing.
"""
import argparse
import glob
import io
import os

import pandas as pd
import requests

DEFAULT = ["NETWEB", "SOLARINDS"]
LOG = "flow_watch_deals.csv"
INST_HINTS = ("MUTUAL FUND", "FUND", "INSURANCE", "LIFE", "ASSURANCE", "PENSION", "BANK", "CAPITAL",
              "INVESTMENT", "SOCIETE", "MORGAN", "GOLDMAN", "CITI", "NOMURA", "GOVERNMENT", "TRUST",
              "MASTER", "FPI", "FII", "SINGAPORE", "LUXEMBOURG", "MAURITIUS", "ABU DHABI", "LLC", "PTE")


def delivery(sym, n=60):
    rows = []
    for f in sorted(glob.glob("delivery_cache/2*.parquet"))[-n:]:
        d = pd.read_parquet(f)
        r = d[(d.SYMBOL == sym) & (d.SERIES == "EQ")]
        if len(r):
            r = r.iloc[0]
            rows.append((os.path.basename(f)[:8], r.CLOSE_PRICE, r.PREV_CLOSE, r.TTL_TRD_QNTY, r.DELIV_QTY, r.DELIV_PER))
    return pd.DataFrame(rows, columns=["date", "close", "prev", "traded", "deliv_qty", "deliv_pct"])


def delivery_report(sym):
    d = delivery(sym)
    if len(d) < 25:
        print(f"{sym}: only {len(d)} sessions of delivery data")
        return
    t, base = d.iloc[-1], d.iloc[-21:-1]
    qx = t.deliv_qty / base.deliv_qty.mean()
    dp = t.deliv_pct - base.deliv_pct.mean()
    up = t.close >= t.prev
    spike = qx >= 1.5 and dp >= 5
    tag = ("ACCUMULATION" if up else "DISTRIBUTION") if spike else "-"
    print(f"{sym} {t.date}: close {t.close:.1f} ({100 * (t.close / t.prev - 1):+.2f}%)  delivered {t.deliv_qty:,.0f} "
          f"= {qx:.2f}x 20d mean  delivery {t.deliv_pct:.1f}% ({dp:+.1f} pts vs {base.deliv_pct.mean():.1f}%)  SPIKE: {tag}")
    print("  last 5: " + " | ".join(f"{r.date[4:]} {r.deliv_pct:.0f}% {r.deliv_qty / base.deliv_qty.mean():.1f}x"
                                    f" {'up' if r.close >= r.prev else 'dn'}" for r in d.tail(5).itertuples()))


def deals(symbols):
    found = []
    for kind in ("bulk", "block"):
        try:
            r = requests.get(f"https://nsearchives.nseindia.com/content/equities/{kind}.csv",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            if r.status_code != 200 or "NO RECORDS" in r.text[:200]:
                continue
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip() for c in df.columns]
            df["Symbol"] = df["Symbol"].astype(str).str.strip()
            df = df[df["Symbol"].isin(symbols)].copy()
            df["kind"] = kind
            found.append(df)
        except Exception as e:
            print(f"{kind} deals: fetch failed ({e})")
    if not found:
        print("Deals: no bulk/block deals in the latest NSE file for these symbols.")
        return
    out = pd.concat(found, ignore_index=True)
    if out.empty:
        print("Deals: no bulk/block deals in the latest NSE file for these symbols.")
        return
    out["inst_hint"] = out["Client Name"].str.upper().apply(lambda s: any(h in s for h in INST_HINTS))
    print(out[["Date", "Symbol", "kind", "Client Name", "Buy/Sell", "Quantity Traded",
               "Trade Price / Wght. Avg. Price", "inst_hint"]].to_string(index=False))
    old = pd.read_csv(LOG) if os.path.exists(LOG) else pd.DataFrame()
    allr = pd.concat([old, out], ignore_index=True).drop_duplicates(
        subset=["Date", "Symbol", "kind", "Client Name", "Buy/Sell", "Quantity Traded"])
    allr.to_csv(LOG, index=False)


def holdings(symbols):
    fs = sorted(glob.glob("fundamentals/fundamentals_*.csv"), reverse=True)
    for sym in symbols:
        for f in fs:
            t = pd.read_csv(f)
            t = t[t.Symbol == sym]
            if len(t):
                r = t.iloc[0].to_dict()
                print(f"{sym} {r['SHP_Qtr']}: MF {r['MF%']}% ({r['MF_chg']:+.2f}), DII {r['DII%']}% "
                      f"({r['DII_chg']:+.2f}), FPI {r['FPI%']}% ({r['FPI_chg']:+.2f}) pts QoQ  [{os.path.basename(f)}]")
                break
        else:
            print(f"{sym}: not in any fundamentals table (run fundamentals.py {sym})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=DEFAULT)
    a = ap.parse_args()
    print("== DELIVERY ==")
    for s in a.symbols:
        delivery_report(s)
    print("== BULK / BLOCK DEALS ==")
    deals(a.symbols)
    print("== LATEST QUARTERLY HOLDINGS ==")
    holdings(a.symbols)

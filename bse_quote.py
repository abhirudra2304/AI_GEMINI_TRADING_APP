"""Daily prices for BSE-only listings (e.g. NSE Ltd itself, which cannot list on its own exchange).

The rest of the system only reads NSE data, so a stock traded solely on BSE is invisible to it.
Reads BSE's public equity bhavcopy; read-only, caches each day's file in bse_cache/ (git-ignored).

    python bse_quote.py NSE            # last 10 sessions
    python bse_quote.py NSE --days 20
"""
import argparse
import io
import os
from datetime import date, timedelta

import pandas as pd
import requests

CACHE = "bse_cache"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bseindia.com/"}


def _day(d: date):
    path = os.path.join(CACHE, f"{d:%Y%m%d}.parquet")
    if os.path.exists(path):
        return pd.read_parquet(path)
    r = requests.get(f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV",
                     headers=HEADERS, timeout=30)
    if r.status_code != 200 or r.content[:2] == b"PK" or b"TckrSymb" not in r.content[:500]:
        return None
    df = pd.read_csv(io.StringIO(r.text))
    df["TckrSymb"] = df["TckrSymb"].astype(str).str.strip()
    df = df[["TradDt", "FinInstrmId", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric",
             "PrvsClsgPric", "TtlTradgVol", "TtlTrfVal"]]
    if d < date.today():  # never cache today's file - it may be provisional
        os.makedirs(CACHE, exist_ok=True)
        df.to_parquet(path, index=False)
    return df


def history(symbol: str, days: int = 10) -> pd.DataFrame:
    rows, d, tries = [], date.today(), 0
    while len(rows) < days and tries < days * 2 + 10:
        if d.weekday() < 5:
            df = _day(d)
            if df is not None:
                rows.append(df[df.TckrSymb == symbol.upper()])
        d -= timedelta(days=1); tries += 1
    out = pd.concat(rows) if rows else pd.DataFrame()
    return out.sort_values("TradDt").reset_index(drop=True) if not out.empty else out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("symbols", nargs="+")
    p.add_argument("--days", type=int, default=10)
    a = p.parse_args()
    for s in a.symbols:
        h = history(s, a.days)
        print(f"\n{s.upper()} (BSE)" + ("" if not h.empty else " - not found in BSE bhavcopy"))
        if not h.empty:
            h["Turnover_cr"] = (h.pop("TtlTrfVal") / 1e7).round(1)
            print(h.drop(columns=["FinInstrmId", "TckrSymb", "SctySrs"]).to_string(index=False))

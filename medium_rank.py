"""Monthly medium-term (1-2 month) ranking of the scan universe.

Standalone and read-only like strategy_paper.py: reads delivery_cache/ (NSE bhavcopy),
fetches recent results filings from NSE's announcements feed (cached), and writes only
medium_rank/medium_rank_<date>.csv. No broker API calls, no scanner changes.

    python medium_rank.py              # rank on the latest settled bhavcopy
    python medium_rank.py --top 30     # longer tier-B list

Run monthly (with `strategy_paper.py rebalance`), after `strategy_paper.py update`.

Score = mean percentile (within the universe) of the two C1 inputs, which are the only
rule that passed the 2026-09-25 backtests: 12-1m momentum and delivery-% surge.
Tier A  = top 30% on BOTH (the C1 gate)
Tier B  = score >= 60
AVOID   = any of: below or under a declining 200-DMA (Stage 2 fails); results reaction
          <= -5% within 60 sessions (negative drift, 2021-26 event study: -2.25%/60d, every
          year); THIN/ILLIQUID liquidity; NO_BTST tradability (BE / ASM III+ / GSM).
Results tag: 2-day return vs the equal-weight universe around the latest filing -
BEAT (>= +5% on >= 2x volume), BAD (<= -5%), flat. A ranked research list, not buy calls:
the system has no fundamental data, so check fundamentals on the shortlist by hand.
"""
import argparse
import json
import os
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import requests

import config
import strategy_paper as sp
from liquidity import load_liquidity
from nse_announcements import NSE_HEADERS

OUT_DIR = "medium_rank"
RESULTS_CACHE = "results_filings_cache.json"
HOLDINGS = ["DATAPATTNS", "DIXON", "SOLARINDS", "TCS", "BEL", "BDL", "HAL", "KAYNES", "CONCOR", "GODFRYPHLP"]
RESULT_DESCS = ("Outcome of Board Meeting", "Financial Result Updates", "Financial Results")


def _results_filings(days=120):
    """Results filings for the last `days` calendar days, {"YYYY-MM-DD": [rows]} cached per
    3-day window; windows that ended before yesterday are never re-fetched."""
    cache = json.load(open(RESULTS_CACHE)) if os.path.exists(RESULTS_CACHE) else {}
    s = None
    d, end = date.today() - timedelta(days=days), date.today()
    while d <= end:
        e = min(d + timedelta(days=2), end)
        key = d.isoformat()
        if key not in cache or e >= date.today() - timedelta(days=1):
            if s is None:
                s = requests.Session(); s.headers.update(NSE_HEADERS)
                s.get("https://www.nseindia.com", timeout=20)
            try:
                r = s.get("https://www.nseindia.com/api/corporate-announcements",
                          params={"index": "equities", "from_date": d.strftime("%d-%m-%Y"), "to_date": e.strftime("%d-%m-%Y")},
                          timeout=90).json()
                cache[key] = [{"symbol": x["symbol"], "an_dt": x["an_dt"]} for x in r
                              if x.get("desc") in RESULT_DESCS and "result" in (x.get("attchmntText") or "").lower()]
                time.sleep(1.0)
            except Exception as ex:
                print(f"  results fetch {key} failed ({ex}) - using cache if any")
        d = e + timedelta(days=1)
    json.dump(cache, open(RESULTS_CACHE, "w"))
    rows = [r for v in cache.values() for r in v]
    df = pd.DataFrame(rows, columns=["symbol", "an_dt"])
    df["ts"] = pd.to_datetime(df.an_dt, format="%d-%b-%Y %H:%M:%S", errors="coerce")
    return df.dropna(subset=["ts"]).sort_values("ts").drop_duplicates("symbol", keep="last")


def _tradability():
    try:
        from tradability import load_status
        t, _ = load_status()
        return t.set_index("Symbol")["Tradability_Flag"] if t is not None and not t.empty else pd.Series(dtype=str)
    except Exception:
        return pd.Series(dtype=str)


def build(data=None):
    data = data or sp.load()
    CA, C, TO = data["CA"], data["C"], data["TO"]
    U, m12, surge, _s50, corr, _ex60 = sp.signals(data)
    d = CA.index[-1]
    uni = [s for s in config.Universe.SECTOR_MAP if s in CA.columns]
    s200 = CA.rolling(200, min_periods=180).mean()
    df = pd.DataFrame(index=pd.Index(uni, name="Symbol"))
    df["Sector"] = [config.Universe.SECTOR_MAP[s] for s in uni]
    df["Close"] = C.loc[d, uni]
    df["Mom12_1%"] = (m12.loc[d, uni] * 100).round(1)
    df["DelivSurge"] = surge.loc[d, uni].round(1)
    df["vs200%"] = ((CA.loc[d, uni] / s200.loc[d, uni] - 1) * 100).round(1)
    rising = s200.loc[d, uni] > s200.iloc[-21][uni]
    df["Off52wH%"] = ((CA.loc[d, uni] / CA.iloc[-252:][uni].max() - 1) * 100).round(1)
    df["Ret1m%"] = ((CA.loc[d, uni] / CA.iloc[-22][uni] - 1) * 100).round(1)

    # results reaction: 2-day return vs the equal-weight point-in-time universe
    R = CA.pct_change(fill_method=None)
    R = R.where(R.abs() <= 0.35)
    bench = R.where(U.shift(1, fill_value=False)).mean(axis=1).fillna(0)
    AR = R.sub(bench, axis=0)
    vol = TO / C  # shares traded (turnover is in lakhs; only the ratio is used)
    vmed = vol.rolling(50, min_periods=30).median().shift(1)
    df["Results"], bad, beat = "-", pd.Series(False, index=df.index), pd.Series(False, index=df.index)
    dates = CA.index
    for _, r in _results_filings().iterrows():
        s = r.symbol
        if s not in df.index:
            continue
        after = r.ts.hour * 60 + r.ts.minute >= 15 * 60 + 30
        i = dates.searchsorted(r.ts.normalize(), side="right" if after else "left")
        if i + 1 >= len(dates):
            df.loc[s, "Results"] = f"{r.ts:%m-%d} pending"
            continue
        ear = (1 + AR[s].iloc[i]) * (1 + AR[s].iloc[i + 1]) - 1
        if np.isnan(ear):
            continue
        vr = vol[s].iloc[i] / vmed[s].iloc[i]
        tag = "BEAT" if ear >= 0.05 and vr >= 2 else ("BAD" if ear <= -0.05 else "flat")
        df.loc[s, "Results"] = f"{r.ts:%m-%d} {tag} {ear * 100:+.0f}%"
        age = len(dates) - 1 - i
        bad[s] = tag == "BAD" and age <= 60
        beat[s] = tag == "BEAT" and age <= 25

    df["Liq"] = load_liquidity().set_index("Symbol")["Liq_Flag"].reindex(df.index).fillna("UNKNOWN")
    df["Trad"] = _tradability().reindex(df.index).fillna("UNKNOWN")

    a, b = df["Mom12_1%"].rank(pct=True), df["DelivSurge"].rank(pct=True)
    df["Score"] = ((a + b) / 2 * 100).round(0)
    stage2 = (df["vs200%"] > 0) & rising
    thin = df.Liq.isin(["THIN", "ILLIQUID"])
    nobtst = df.Trad == "NO_BTST"
    df["Why_avoid"] = np.select([~stage2, bad, thin, nobtst],
                                ["below/declining 200DMA", "bad results <60 sessions", "thin liquidity", "BE/surveillance"], "")
    df["Tier"] = np.where(df.Why_avoid != "", "AVOID",
                          np.where((a >= 0.7) & (b >= 0.7), "A", np.where(df.Score >= 60, "B", "C")))
    df["Recent_Beat"] = beat
    order = {"A": 0, "B": 1, "C": 2, "AVOID": 3}
    df = df.sort_values(["Tier", "Score"], key=lambda c: c.map(order) if c.name == "Tier" else c,
                        ascending=[True, False])
    return d, bool(corr.loc[d]), df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20, help="rows shown for tier B")
    args = ap.parse_args()
    d, correction, df = build()
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"medium_rank_{d:%Y%m%d}.csv")
    df.to_csv(path)

    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 300)
    cols = ["Sector", "Close", "Score", "Mom12_1%", "DelivSurge", "vs200%", "Off52wH%", "Ret1m%", "Results", "Liq"]
    print(f"MEDIUM-TERM RANK - bhavcopy {d:%Y-%m-%d} - universe {len(df)} - "
          f"market correction: {'YES' if correction else 'no'}")
    print("Tiers:", df.Tier.value_counts().reindex(["A", "B", "C", "AVOID"]).fillna(0).astype(int).to_dict(),
          "| avoid reasons:", df[df.Tier == "AVOID"].Why_avoid.value_counts().to_dict())
    print("\n=== TIER A (C1 gate: top 30% momentum AND delivery surge) ===")
    print(df[df.Tier == "A"][cols].to_string())
    print(f"\n=== TIER B (score >= 60), top {args.top} ===")
    print(df[df.Tier == "B"].head(args.top)[cols].to_string())
    rb = df[df.Recent_Beat]
    if len(rb):
        print("\n=== Results BEAT in the last 25 sessions (any tier) ===")
        print(rb[cols + ["Tier", "Why_avoid"]].to_string())
    print("\n=== Holdings ===")
    print(df.loc[[h for h in HOLDINGS if h in df.index], cols + ["Tier", "Why_avoid"]].to_string())
    print(f"\nSaved {path}. Research list, not buy calls - check fundamentals (sales/profit growth, debt, ROCE) "
          "on the shortlist before acting.")


if __name__ == "__main__":
    main()

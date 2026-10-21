"""Liquidity flag: median daily traded value from NSE's official bhavcopy (delivery_cache).

Why: every backtest here assumed a flat 0.25% round-trip cost, which only fits large
caps. Thin names (recent IPOs, KRISHNADEF, small defence/EMS) cost several times that,
and nothing in the pipeline looked at it. Informational only - it never drops a row.

Tiers by median traded value over the last `window` sessions (Rs crore/day):
    OK        >= 50    est. round-trip cost ~0.25%
    MODERATE  10 - 50  ~0.40%
    THIN      2 - 10   ~0.80%
    ILLIQUID  < 2      ~1.50%
    UNKNOWN   fewer than `min_days` sessions of data (new listings)
The cost figures are rough planning assumptions, not measured spreads.
"""
import glob
import os
from typing import Optional

import pandas as pd

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delivery_cache")
TIERS = [(50.0, "OK", 0.25), (10.0, "MODERATE", 0.40), (2.0, "THIN", 0.80), (0.0, "ILLIQUID", 1.50)]


def _tier(adv_cr: float):
    for floor, name, cost in TIERS:
        if adv_cr >= floor:
            return name, cost
    return "ILLIQUID", TIERS[-1][2]


def load_liquidity(window: int = 20, min_days: int = 10, cache_dir: str = _DIR) -> pd.DataFrame:
    """One row per symbol: Symbol, ADV_Cr, Liq_Flag, Est_Cost_Pct, Liq_Days."""
    files = sorted(glob.glob(os.path.join(cache_dir, "2*.parquet")))[-window:]
    frames = []
    for f in files:
        try:
            d = pd.read_parquet(f, columns=["SYMBOL", "SERIES", "TURNOVER_LACS"])
        except Exception:
            continue
        d = d[d["SERIES"].isin(["EQ", "BE", "BZ"])]
        frames.append(d[["SYMBOL", "TURNOVER_LACS"]])
    if not frames:
        return pd.DataFrame(columns=["Symbol", "ADV_Cr", "Liq_Flag", "Est_Cost_Pct", "Liq_Days"])
    allr = pd.concat(frames)
    allr["cr"] = pd.to_numeric(allr["TURNOVER_LACS"], errors="coerce") / 100.0
    g = allr.groupby("SYMBOL")["cr"]
    out = pd.DataFrame({"ADV_Cr": g.median(), "Liq_Days": g.count()}).reset_index().rename(columns={"SYMBOL": "Symbol"})
    tiers = out["ADV_Cr"].fillna(0).map(_tier)
    out["Liq_Flag"] = [t[0] for t in tiers]
    out["Est_Cost_Pct"] = [t[1] for t in tiers]
    short = out["Liq_Days"] < min_days
    out.loc[short, "Liq_Flag"] = "UNKNOWN"
    out.loc[short, "Est_Cost_Pct"] = float("nan")
    out["ADV_Cr"] = out["ADV_Cr"].round(1)
    return out


def annotate_liquidity(df: pd.DataFrame, liq: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Left-join Liq_Flag / ADV_Cr / Est_Cost_Pct onto a frame with a Symbol column."""
    out = df.drop(columns=[c for c in ("ADV_Cr", "Liq_Flag", "Est_Cost_Pct", "Liq_Days") if c in df.columns])
    if liq is None or liq.empty or "Symbol" not in out.columns:
        out["Liq_Flag"] = "UNKNOWN"
        return out
    out = out.merge(liq[["Symbol", "ADV_Cr", "Liq_Flag", "Est_Cost_Pct"]], on="Symbol", how="left")
    out["Liq_Flag"] = out["Liq_Flag"].fillna("UNKNOWN")
    return out


if __name__ == "__main__":
    import config
    liq = load_liquidity()
    u = liq[liq["Symbol"].isin(config.Universe.SECTOR_MAP.keys())]
    print(f"universe symbols with data: {len(u)} of {len(config.Universe.SECTOR_MAP)}")
    print(u["Liq_Flag"].value_counts().to_string())
    print("\nweakest 25 (Rs cr/day):")
    print(u.sort_values("ADV_Cr").head(25)[["Symbol", "ADV_Cr", "Liq_Flag", "Est_Cost_Pct"]].to_string(index=False))

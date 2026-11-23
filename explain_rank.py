"""Read-only Rank_Score breakdown for one symbol (mirrors discovery.py:299-315).

Usage: python explain_rank.py SYMBOL [BTST|SWING|GAP|INTRADAY]
Reads discovery_cache_<strategy>.pkl; makes no API calls and writes nothing.
Rows absent from the cache were dropped by discovery's hard filters
(liquidity, EMA trend, RS_Pctl, RSI) - see FILTER NOTE in output.
"""
import os
import pickle
import sys

import pandas as pd

WEIGHTS = (("ADX", 0.4, 50), ("RS_Pctl", 0.8, 100), ("Sector_RS", 0.4, 100), ("RSI", 0.2, 80))
STRATEGIES = ("BTST", "SWING", "GAP", "INTRADAY")


def explain_row(row) -> dict:
    """Factor breakdown for one discovered_df row. Pure function."""
    terms = []
    for col, w, cap in WEIGHTS:
        raw = float(row[col])
        capped = min(raw, cap)
        terms.append({"factor": col, "raw": raw, "cap": cap, "weight": w, "contribution": capped * w})
    numerator = sum(t["contribution"] for t in terms)
    dist = float(row["Distance"])
    div_normal = max(1.0, abs(dist) * 0.5)
    div_shock = max(1.0, abs(dist) * 0.2)
    rank = float(row["Rank_Score"])
    # Volume shock is not persisted; infer it from which divisor reproduces Rank_Score.
    shock = abs(numerator / div_shock - rank) < abs(numerator / div_normal - rank)
    divisor = div_shock if shock else div_normal
    return {
        "terms": terms,
        "numerator": numerator,
        "distance_pct": dist,
        "divisor": divisor,
        "volume_shock_inferred": shock,
        "rank_recomputed": numerator / divisor,
        "rank_cached": rank,
    }


def render(symbol: str, strategy: str, row, ex: dict) -> str:
    out = [f"RANK_SCORE BREAKDOWN  {symbol}  [{strategy}]  regime={row.get('Market_Regime', 'n/a')}",
           "=" * 78,
           f"{'Factor':<11}{'Raw':>9}{'Cap':>6}{'Weight':>8}{'Contrib':>10}",
           "-" * 78]
    for t in ex["terms"]:
        out.append(f"{t['factor']:<11}{t['raw']:>9.2f}{t['cap']:>6}{t['weight']:>8.1f}{t['contribution']:>10.2f}")
    out += ["-" * 78,
            f"{'Numerator':<34}{ex['numerator']:>10.2f}",
            f"{'Pivot_50 distance %':<34}{ex['distance_pct']:>10.2f}",
            f"{'Divisor (' + ('vol-shock 0.2x' if ex['volume_shock_inferred'] else 'normal 0.5x') + ')':<34}{ex['divisor']:>10.2f}",
            f"{'Rank_Score (recomputed)':<34}{ex['rank_recomputed']:>10.2f}",
            f"{'Rank_Score (cached)':<34}{ex['rank_cached']:>10.2f}",
            "=" * 78,
            "NOTE: descriptive only. Rank_Score is a ranking input, not a forecast; no proven edge (see memory).",
            ]
    return "\n".join(out)


def load_frame(strategy: str, root: str = ".") -> pd.DataFrame | None:
    path = os.path.join(root, f"discovery_cache_{strategy.lower()}.pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        payload = pickle.load(f)
    return payload.get("discovered_df")


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    symbol = argv[0].upper()
    strategies = [argv[1].upper()] if len(argv) > 1 else list(STRATEGIES)
    found = False
    for strat in strategies:
        df = load_frame(strat)
        if df is None or df.empty:
            continue
        hit = df[df["Symbol"] == symbol]
        if hit.empty:
            continue
        found = True
        row = hit.iloc[0]
        print(render(symbol, strat, row, explain_row(row)))
        print()
    if not found:
        print(f"{symbol}: not in any cached discovery frame ({', '.join(strategies)}).")
        print("FILTER NOTE: dropped by discovery hard filters (20d traded value < 10cr, close < EMA50,")
        print("RS_Pctl < threshold, RSI < threshold) unless a volume shock applied, or cache is stale.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

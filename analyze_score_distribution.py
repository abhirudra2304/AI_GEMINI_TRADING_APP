import sqlite3
import pandas as pd
import numpy as np
import os
import sys

# Add the project root to the Python path to resolve import issues
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- AUTO VENV ACTIVATION ---
from venv_activator import ensure_venv
ensure_venv()
# --------------------------

DB = "signals.db"

def main():
    conn = sqlite3.connect(DB)

    df = pd.read_sql("""
    SELECT
        s.id,
s.new_score AS score,
        s.rs_pctl,
        s.sector_rs,
        s.adx,
        s.vol_ratio,
        o.outcome,
        o.return
    FROM signals s
    LEFT JOIN outcomes o
        ON s.id = o.signal_id
    """, conn)

    conn.close()

    df = df[df["score"].notna()].copy()

    if df.empty:
        print("No signals with score found.")
        return

    print("\n" + "=" * 80)
    print("SCORE DISTRIBUTION")
    print("=" * 80)

    print(f"Count:   {len(df):,}")
    print(f"Min:     {df['score'].min():.1f}")
    print(f"Max:     {df['score'].max():.1f}")
    print(f"Mean:    {df['score'].mean():.1f}")
    print(f"Median:  {df['score'].median():.1f}")
    print(f"Std Dev: {df['score'].std():.1f}")

    print("\nSCORE DECILES (10th, 20th, ... 90th percentiles):")

    deciles = np.percentile(
        df["score"],
        [10,20,30,40,50,60,70,80,90]
    )

    for i, d in enumerate(deciles, start=1):
        print(f"  {i*10}th percentile: {d:.1f}")

    print("\n" + "=" * 80)
    print("DECILE ANALYSIS (Score vs Factors)")
    print("=" * 80)

    # Safe qcut handling duplicate edges
    df["decile"] = pd.qcut(
        df["score"],
        q=10,
        duplicates="drop"
    )

    decile_stats = []

    for dec in df["decile"].cat.categories:

        sub = df[df["decile"] == dec]

        count = len(sub)

        score_range = (
            f"{sub['score'].min():.1f}-"
            f"{sub['score'].max():.1f}"
        )

        avg_rs = sub["rs_pctl"].mean()
        avg_sec = sub["sector_rs"].mean()
        avg_adx = sub["adx"].mean()
        avg_vol = sub["vol_ratio"].mean()

        ret_data = sub["return"].dropna()

        if len(ret_data) > 0:
            win_rate = (ret_data > 0).mean()
            avg_ret = ret_data.mean()
        else:
            win_rate = np.nan
            avg_ret = np.nan

        decile_stats.append({
            "Decile": str(dec),
            "Score_Range": score_range,
            "Count": count,
            "Avg_RS": avg_rs,
            "Avg_SectorRS": avg_sec,
            "Avg_ADX": avg_adx,
            "Avg_VolRatio": avg_vol,
            "Win_Rate": win_rate,
            "Avg_Return": avg_ret
        })

    decile_df = pd.DataFrame(decile_stats)

    pd.set_option(
        "display.float_format",
        lambda x: f"{x:.3f}" if pd.notna(x) else "nan"
    )

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)

    print(decile_df.to_string(index=False))

    print("\n" + "=" * 80)
    print("FACTOR CORRELATION WITH SCORE")
    print("=" * 80)

    corr_rs = df[["score", "rs_pctl"]].corr().iloc[0, 1]
    corr_sec = df[["score", "sector_rs"]].corr().iloc[0, 1]
    corr_adx = df[["score", "adx"]].corr().iloc[0, 1]
    corr_vol = df[["score", "vol_ratio"]].corr().iloc[0, 1]

    print(f"Score vs RS Percentile:  {corr_rs:+.4f}")
    print(f"Score vs Sector RS:      {corr_sec:+.4f}")
    print(f"Score vs ADX:            {corr_adx:+.4f}")
    print(f"Score vs Volume Ratio:   {corr_vol:+.4f}")

    print("\n" + "=" * 80)
    print("SCORE BUCKETS vs OUTCOMES")
    print("=" * 80)

    buckets = [
        (0,20),
        (20,40),
        (40,60),
        (60,80),
        (80,100)
    ]

    for lo, hi in buckets:

        sub = df[
            (df["score"] >= lo) &
            (df["score"] < hi)
        ]

        if len(sub) == 0:
            print(f"{lo:2d}-{hi:2d}: --")
            continue

        ret = sub["return"].dropna()

        if len(ret) > 0:
            wr = (ret > 0).mean()
            ar = ret.mean()
        else:
            wr = np.nan
            ar = np.nan

        print(
            f"{lo:2d}-{hi:2d}: "
            f"n={len(sub):4d}, "
            f"win_rate={wr:.3f}, "
            f"avg_return={ar:.4f}"
        )

if __name__ == "__main__":
    main()
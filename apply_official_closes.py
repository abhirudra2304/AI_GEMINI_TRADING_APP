"""Overwrite cached daily closes with NSE's official close from the bhavcopy in delivery_cache/.

Angel's daily candle close is the last traded price, and a bar fetched mid-session can
survive as the day's close when a later refetch fails. NSE's bhavcopy has the official
close. Run after `strategy_paper.py update` has pulled the day's file.

Only bars on the same price basis are touched: if cache and NSE differ by more than
MAX_BASIS_GAP, the cached series is split/bonus-adjusted (Angel adjusts history, the
bhavcopy doesn't) and the bar is left alone. File mtimes are preserved because
data_broker uses mtime to decide whether a bar is settled.

    python apply_official_closes.py            # apply
    python apply_official_closes.py --dry-run  # report only
"""
import argparse
import glob
import os

import pandas as pd

import config

DELIVERY_DIR = "delivery_cache"
HISTORY_DIR = "historical_data"
MAX_BASIS_GAP = 0.02   # 2%: above this the two series are on different split bases
MIN_FIX = 0.0005       # ignore sub-0.05% rounding differences


def load_official_closes() -> pd.DataFrame:
    frames = []
    for path in sorted(glob.glob(os.path.join(DELIVERY_DIR, "20*.parquet"))):
        d = pd.read_parquet(path, columns=["SYMBOL", "SERIES", "CLOSE_PRICE"])
        d = d[d["SERIES"] == "EQ"]
        d["date"] = pd.Timestamp(os.path.basename(path)[:8]).date()
        frames.append(d[["SYMBOL", "date", "CLOSE_PRICE"]])
    return pd.concat(frames).drop_duplicates(["SYMBOL", "date"], keep="last")


def fix_file(path: str, official: dict, dry_run: bool) -> int:
    df = pd.read_parquet(path)
    if df.empty or "Timestamp" not in df.columns:
        return 0
    dates = pd.to_datetime(df["Timestamp"]).dt.date
    nse = dates.map(official)
    gap = (df["Close"] / nse - 1).abs()
    mask = nse.notna() & (gap > MIN_FIX) & (gap <= MAX_BASIS_GAP)
    n = int(mask.sum())
    if n and not dry_run:
        df.loc[mask, "Close"] = nse[mask].astype(float)
        df["High"] = df[["High", "Close"]].max(axis=1)
        df["Low"] = df[["Low", "Close"]].min(axis=1)
        st = os.stat(path)
        df.to_parquet(path, index=False)
        os.utime(path, (st.st_atime, st.st_mtime))
    return n


def run(dry_run: bool = False) -> int:
    off = load_official_closes()
    by_sym = {s: dict(zip(g["date"], g["CLOSE_PRICE"])) for s, g in off.groupby("SYMBOL")}
    total, files = 0, 0
    for sym in config.Universe.TARGET_UNIVERSE:
        if sym not in by_sym:
            continue
        for path in glob.glob(os.path.join(HISTORY_DIR, f"{sym}_ONE_DAY*.parquet")):
            n = fix_file(path, by_sym[sym], dry_run)
            if n:
                total += n
                files += 1
    verb = "would correct" if dry_run else "corrected"
    print(f"{verb} {total} daily closes in {files} files (latest NSE file: {max(off['date'])})")
    return total


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true")
    run(p.parse_args().dry_run)

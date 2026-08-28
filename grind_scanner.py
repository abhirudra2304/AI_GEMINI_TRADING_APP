"""Multi-week "grind" detector - finds steady compounders the daily scan can't see.

Built 2026-08-28 after PTC Industries (PTCIL) was found to have quietly gained
~23% in a month while never ranking near the top of `momentum`. Root cause: the
momentum report's only relative-strength column is `RS_vs_Nifty`, and it is a
ONE-DAY measure (see the "RS vs Nifty (1D)" text in its own Reason string).
A stock drifting up ~1%/day for a month never prints a big 1-day RS, so it stays
invisible to the daily ranking no matter how strong the underlying trend is.

This module ranks on the trailing multi-week return instead, and separates a
genuine grind from a single-spike move.

WHY THIS RANKS ON RETURN RATHER THAN A WEIGHTED "QUALITY SCORE"
--------------------------------------------------------------
A weighted 7-component stealth-accumulation score (log-regression slope, ATR
percentile, EMA-ride tightness, spike absence, volume steadiness, base
tightness) was proposed and walk-forward tested on 2026-08-28: 14,569
observations, 543 symbols, forward 20-day EXCESS return (cross-sectional, so
market regime is removed). Result: the composite scored IC -0.087 - inversely
predictive - and its top quintile had the WORST forward returns (-1.50% mean
excess, 39.2% win rate) while its bottom quintile had the best (+1.62%). Three
of its components (ATR percentile, slope, EMA extension) had signs opposite to
what the data showed. That test had real limits (one ~1yr regime, overlapping
windows, 32 distinct dates) so it does NOT prove the reverse weighting would
work either - it only proves the weights were unvalidated priors. Rather than
re-fit weights on a single regime (overfitting), this module ranks on plain
trailing return and reports the descriptive shape measures ALONGSIDE it, so a
human can judge quality without a black-box score deciding for them.

The descriptive measures kept (R-squared of the log-price regression, up-day
count, return-excluding-best-day) are genuinely informative about trend shape.
They are reported, never used to rank or filter - the same informational-only
convention as Institutional_Score and Reliability_Flag.

COST: zero API calls. Reads the daily parquet cache that momentum/discovery
already populate, so a full 215-symbol pass takes seconds rather than the
~30 minutes an API-based version needed (measured: an API version timed out at
10 minutes; this reads 215 symbols in under 15 seconds).
"""
import logging
import os
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

GRIND_OUTPUT_PATH = "grind_candidates.csv"
HISTORICAL_DIR = "historical_data"

# Cache files are written per (symbol, interval, days_back); prefer the widest
# daily window available so the regression has room.
_DAILY_CACHE_SUFFIXES = [
    "_ONE_DAY_400.parquet",
    "_ONE_DAY_460.parquet",
    "_ONE_DAY_200.parquet",
    "_ONE_DAY.parquet",
]

TREND_WINDOW = 20          # trading days the grind is measured over
SHORT_WINDOW = 10          # used to detect a grind that has already stalled
STAGE2_MA = 150            # ~30 weeks of trading days
STAGE2_SLOPE_LOOKBACK = 10 # bars used to decide the long MA is rising
MIN_RETURN_PCT = 10.0      # below this it is not a move worth calling a grind
HIDDEN_RS_MAX = 40.0       # 1-day RS under this = invisible to the daily ranking
QUALITY_UP_DAYS = 13       # of TREND_WINDOW; a real grind closes up most days
STALL_SHORT_RET = 0.0      # 20d strong but 10d negative => the move is over


def _daily_cache_path(symbol: str) -> Optional[str]:
    for suffix in _DAILY_CACHE_SUFFIXES:
        path = os.path.join(HISTORICAL_DIR, f"{symbol}{suffix}")
        if os.path.exists(path):
            return path
    return None


def _log_regression_r2(close: pd.Series) -> float:
    """R-squared of a straight line fit to log(price).

    Log space matters: the slope becomes a compounding daily return, so a
    Rs.100 stock and a Rs.20,000 stock are directly comparable. R-squared is
    the useful part here - it separates a smooth drift from a gap-and-chop
    move that happens to end at the same price.
    """
    y = np.log(close.astype(float).values)
    if len(y) < 3 or not np.all(np.isfinite(y)):
        return float("nan")
    x = np.arange(len(y), dtype=float)
    try:
        slope, intercept = np.polyfit(x, y, 1)
    except (np.linalg.LinAlgError, ValueError):
        return float("nan")
    fitted = slope * x + intercept
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot <= 0:
        return float("nan")
    return round(1.0 - ss_res / ss_tot, 3)


def stage2_state(close: pd.Series) -> Optional[bool]:
    """Weinstein Stage 2: price above a RISING ~30-week moving average.

    Measured on daily bars (150 sessions ~= 30 weeks) rather than by
    resampling to weekly - same window, and it avoids depending on where the
    week boundary happens to fall relative to the last bar.

    Validated 2026-08-28 before being added, on 4,047 observations across 213
    symbols using forward 20-day EXCESS return (cross-sectional, so market
    regime is removed):

        Stage 2 true : +0.53% mean excess, 45.7% win rate  (n=1585)
        Stage 2 false: -0.34% mean excess, 42.1% win rate  (n=2462)
        edge +0.88pp, Welch p=0.013

    Caveats recorded honestly: one ~1yr regime, overlapping forward windows
    (so n is inflated and the p-value is optimistic), 19 distinct dates. That
    is enough to justify REPORTING it, not enough to gate trades on it - so
    it is informational only here, the same convention as Institutional_Score
    and Reliability_Flag. Returns None when history is too short to judge.
    """
    if len(close) < STAGE2_MA + STAGE2_SLOPE_LOOKBACK:
        return None
    ma = close.rolling(STAGE2_MA).mean()
    if pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-1 - STAGE2_SLOPE_LOOKBACK]):
        return None
    rising = ma.iloc[-1] > ma.iloc[-1 - STAGE2_SLOPE_LOOKBACK]
    return bool(close.iloc[-1] > ma.iloc[-1] and rising)


def build_grind_table(momentum_report_path: Optional[str] = None) -> pd.DataFrame:
    """One row per universe symbol with a readable daily cache.

    `momentum_report_path` is optional - when given, the 1-day RS and score
    from that report are joined on so the "strong multi-week move, invisible
    1-day RS" cases can be flagged. Without it the shape measures still work.
    """
    rs_1d, score_map = {}, {}
    if momentum_report_path and os.path.exists(momentum_report_path):
        try:
            mom = pd.read_csv(momentum_report_path)
            rs_1d = dict(zip(mom["Symbol"], mom["RS_vs_Nifty"]))
            score_map = dict(zip(mom["Symbol"], mom["EMFB_Score"]))
        except Exception as e:  # a malformed report must not kill the scan
            logger.warning(f"Could not read {momentum_report_path} ({e}); continuing without RS join.")

    rows, missing = [], 0
    for symbol in config.Universe.TARGET_UNIVERSE:
        path = _daily_cache_path(symbol)
        if path is None:
            missing += 1
            continue
        try:
            df = pd.read_parquet(path)
        except Exception as e:
            logger.warning(f"Unreadable cache for {symbol} ({e}); skipped.")
            missing += 1
            continue

        if "Close" not in df.columns or len(df) < TREND_WINDOW + 2:
            missing += 1
            continue
        if "Timestamp" in df.columns:
            df = df.sort_values("Timestamp")

        close = df["Close"].astype(float).reset_index(drop=True)
        if (close <= 0).any():
            missing += 1
            continue

        ret20 = (close.iloc[-1] / close.iloc[-(TREND_WINDOW + 1)] - 1) * 100
        ret10 = (close.iloc[-1] / close.iloc[-(SHORT_WINDOW + 1)] - 1) * 100
        daily = close.pct_change().tail(TREND_WINDOW) * 100
        up_days = int((daily > 0).sum())
        max_day = float(daily.max())
        # Return with the single best day removed. A grind keeps most of its
        # gain here; a one-spike move collapses.
        ret20_ex_best = ret20 - max_day
        r2 = _log_regression_r2(close.tail(TREND_WINDOW))
        stage2 = stage2_state(close)

        last_ts = None
        if "Timestamp" in df.columns:
            last_ts = pd.Timestamp(df["Timestamp"].iloc[-1]).date()

        rows.append({
            "Symbol": symbol,
            "Sector": config.Universe.SECTOR_MAP.get(symbol, "OTHER"),
            "Last_Bar": last_ts,
            "Ret20d_Pct": round(ret20, 1),
            "Ret10d_Pct": round(ret10, 1),
            "Up_Days_20": up_days,
            "Max_Day_Pct": round(max_day, 1),
            "Ret20_ex_Best": round(ret20_ex_best, 1),
            "LogFit_R2": r2,
            "Stage2": stage2,
            "RS_1D": round(float(rs_1d[symbol]), 1) if symbol in rs_1d else np.nan,
            "Momentum_Score": round(float(score_map[symbol]), 1) if symbol in score_map else np.nan,
            "In_Momentum_Scan": symbol in score_map,
        })

    if missing:
        logger.info(f"Grind scan: {missing} symbol(s) had no usable daily cache (run a scan first to populate).")
    return pd.DataFrame(rows)


def annotate_grind(df: pd.DataFrame) -> pd.DataFrame:
    """Adds Grind_Flag. Labels only - never drops or reorders rows.

    HIDDEN   - real multi-week move the 1-day RS ranking cannot see (the PTCIL case)
    STALLING - 20-day move is strong but the last 10 days have rolled over
    QUALITY  - consistent, not carried by one spike
    GRIND    - clears the return bar without the above qualifiers
    """
    if df.empty:
        return df
    out = df.copy()

    strong = out["Ret20d_Pct"] >= MIN_RETURN_PCT
    hidden = strong & out["RS_1D"].notna() & (out["RS_1D"] < HIDDEN_RS_MAX)
    stalling = strong & (out["Ret10d_Pct"] <= STALL_SHORT_RET)
    quality = (
        strong
        & (out["Up_Days_20"] >= QUALITY_UP_DAYS)
        & (out["Ret20_ex_Best"] >= out["Ret20d_Pct"] * 0.7)
    )

    flag = pd.Series("", index=out.index)
    flag[strong] = "GRIND"
    flag[quality] = "QUALITY"
    flag[hidden] = "HIDDEN"
    flag[stalling] = "STALLING"  # takes precedence: a stalled move is not an opportunity
    out["Grind_Flag"] = flag
    return out


def annotate_reliability(df: pd.DataFrame) -> pd.DataFrame:
    """Joins the per-symbol resolved-trade record. Fail-soft: a missing or
    broken reliability table degrades to INSUFFICIENT_DATA rather than
    taking down the scan (same convention as stock_scan.py's phases)."""
    out = df.copy()
    try:
        from symbol_reliability import build_reliability_table
        table = build_reliability_table()
        if table.empty:
            raise ValueError("empty reliability table")
        out = out.merge(
            table[["Symbol", "Reliability_Flag", "Trades", "Win_Rate", "Avg_Return_Pct"]],
            on="Symbol", how="left",
        )
    except Exception as e:
        logger.warning(f"Reliability annotation skipped ({e}).")
        for col in ["Reliability_Flag", "Trades", "Win_Rate", "Avg_Return_Pct"]:
            out[col] = np.nan
    out["Reliability_Flag"] = out["Reliability_Flag"].fillna("INSUFFICIENT_DATA")
    return out


def build_grind_report(momentum_report_path: Optional[str] = None) -> pd.DataFrame:
    """End-to-end: read cache -> measure -> flag -> annotate -> save."""
    table = build_grind_table(momentum_report_path)
    if table.empty:
        return table
    table = annotate_reliability(annotate_grind(table))
    table = table.sort_values("Ret20d_Pct", ascending=False).reset_index(drop=True)
    try:
        table.to_csv(GRIND_OUTPUT_PATH, index=False)
    except OSError as e:
        logger.warning(f"Failed to save {GRIND_OUTPUT_PATH}: {e}")
    return table


def find_latest_momentum_report() -> Optional[str]:
    import glob
    files = sorted(glob.glob("momentum_report_*.csv"))
    return files[-1] if files else None


def print_grind_report(df: pd.DataFrame, top_n: int = 15) -> None:
    print("\n" + "=" * 108)
    print(f"GRIND SCAN - multi-week steady movers ({TREND_WINDOW}-day), {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 108)
    if df.empty:
        print("No usable daily cache found. Run `python main.py momentum` first to populate it.")
        print("=" * 108)
        return

    cols = ["Symbol", "Sector", "Ret20d_Pct", "Ret10d_Pct", "Up_Days_20", "Ret20_ex_Best",
            "LogFit_R2", "Stage2", "RS_1D", "Reliability_Flag", "Win_Rate"]
    cols = [c for c in cols if c in df.columns]

    hidden = df[df["Grind_Flag"] == "HIDDEN"]
    if not hidden.empty:
        print(f"\n--- HIDDEN ({len(hidden)}) - real multi-week move, but 1-day RS < {HIDDEN_RS_MAX:.0f} so the daily ranking misses it ---")
        print(hidden[cols].head(top_n).to_string(index=False))

    quality = df[df["Grind_Flag"] == "QUALITY"]
    if not quality.empty:
        print(f"\n--- QUALITY ({len(quality)}) - consistent climb, not carried by one spike ---")
        print(quality[cols].head(top_n).to_string(index=False))

    stalling = df[df["Grind_Flag"] == "STALLING"]
    if not stalling.empty:
        print(f"\n--- STALLING ({len(stalling)}) - strong 20-day number, but the last 10 days rolled over ---")
        print(stalling[cols].head(top_n).to_string(index=False))

    absent = df[(df["Grind_Flag"].isin(["GRIND", "QUALITY", "HIDDEN"])) & (~df["In_Momentum_Scan"])]
    if not absent.empty:
        print(f"\n--- NOT IN TODAY'S MOMENTUM REPORT AT ALL ({len(absent)}) ---")
        print(absent[cols].head(top_n).to_string(index=False))

    unreliable = df[(df["Grind_Flag"] != "") & (df["Reliability_Flag"] == "UNRELIABLE")]
    if not unreliable.empty:
        print(f"\n⚠️  GRINDING BUT UNRELIABLE ({len(unreliable)}) - real move, real losing record; treat with caution:")
        for _, r in unreliable.head(10).iterrows():
            print(f"      {r['Symbol']}: +{r['Ret20d_Pct']}% over {TREND_WINDOW}d, but "
                  f"{int(r['Trades'])} resolved trades at {r['Win_Rate']:.0f}% win, {r['Avg_Return_Pct']:+.1f}% avg return")

    print("\n" + "-" * 108)
    print("Ranked by trailing return. LogFit_R2/Up_Days_20/Ret20_ex_Best describe trend SHAPE - informational, not used to rank.")
    print("Ret20_ex_Best = return with the single best day removed (a real grind keeps most of it; a one-spike move collapses).")
    print(f"Full table saved to {GRIND_OUTPUT_PATH}.")
    print("=" * 108)

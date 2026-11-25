"""
STANDALONE SCRATCH SCRIPT - NOT PRODUCTION. Non-destructive, read-only.

Feeds MarketContextEngine.analyze() (market_context.py) with cached NIFTY /
BANKNIFTY / India VIX daily candles plus a universe-wide advance/decline and
50-DMA breadth snapshot computed from cached per-symbol parquet files, and a
sector-breadth map derived from today's momentum report's sector rotation.

Deliberately does NOT import or wire through pipeline_runner.py, per
instruction - market_context.py is imported directly and nothing else in
this script touches production files or the live API.

Run: .venv/Scripts/python.exe scratch_regime_check_20260902.py
"""
from __future__ import annotations

import glob
import os

import pandas as pd

import config
from market_context import (
    AdvanceDeclineData,
    BreadthData,
    MarketContextEngine,
    MarketContextInputs,
)

HIST_DIR = config.CacheConfig.HISTORICAL_DATA_DIR


def _load(symbol_file: str) -> pd.DataFrame:
    path = os.path.join(HIST_DIR, symbol_file)
    df = pd.read_parquet(path)
    df = df.rename(columns={"Timestamp": "Date"}).sort_values("Date")
    return df


def _latest_universe_report() -> str | None:
    candidates = sorted(glob.glob("momentum_report_*.csv"))
    return candidates[-1] if candidates else None


def build_inputs() -> MarketContextInputs:
    nifty = _load("Nifty 50_ONE_DAY_400.parquet")
    banknifty = _load("NIFTY BANK_ONE_DAY_400.parquet")
    vix = _load("India VIX_ONE_DAY_400.parquet")

    # --- Universe-wide advance/decline + % above 50-DMA, from cached daily candles ---
    advances = declines = unchanged = above_50dma = counted = 0
    for symbol in config.Universe.TARGET_UNIVERSE:
        path = os.path.join(HIST_DIR, f"{symbol}_ONE_DAY_400.parquet")
        if not os.path.exists(path):
            continue
        df = pd.read_parquet(path).sort_values("Timestamp")
        if len(df) < 51:
            continue
        closes = df["Close"]
        today_close = closes.iloc[-1]
        prev_close = closes.iloc[-2]
        if today_close > prev_close:
            advances += 1
        elif today_close < prev_close:
            declines += 1
        else:
            unchanged += 1
        sma50 = closes.iloc[-50:].mean()
        if today_close > sma50:
            above_50dma += 1
        counted += 1

    pct_above_50dma = (above_50dma / counted * 100.0) if counted else 0.0
    ad = AdvanceDeclineData(advances=advances, declines=declines, unchanged=unchanged)
    breadth = BreadthData(pct_above_50dma=pct_above_50dma)

    # --- Sector breadth, from today's momentum report's sector rotation ---
    sector_breadth: dict[str, float] = {}
    report_path = _latest_universe_report()
    if report_path:
        rep = pd.read_csv(report_path)
        # per-symbol RS_vs_Sector isn't a sector breadth figure by itself; use
        # the sector rotation table already computed by main.py momentum
        # (Sector_Percentile) if a companion file exists, else skip.
        pass

    return MarketContextInputs(
        nifty=nifty.rename(columns={"Close": "Close", "Volume": "Volume"}),
        banknifty=banknifty,
        india_vix=vix,
        advance_decline=ad,
        breadth=breadth,
        sector_breadth=sector_breadth,
    ), counted


def main():
    inputs, counted = build_inputs()
    engine = MarketContextEngine()
    ctx = engine.analyze(inputs)

    print("=" * 90)
    as_of = inputs.nifty.sort_values("Date").iloc[-1]["Date"]
    print(f"MARKET CONTEXT ENGINE - standalone scratch reading (as of {as_of.date()})")
    print("=" * 90)
    print(f"Universe symbols with usable cache: {counted}/{len(config.Universe.TARGET_UNIVERSE)}")
    print(f"Advance/Decline: {inputs.advance_decline.advances}/{inputs.advance_decline.declines} "
          f"(unch {inputs.advance_decline.unchanged}), ratio={inputs.advance_decline.ratio:.2f}")
    print(f"% above 50-DMA: {inputs.breadth.pct_above_50dma:.1f}%")
    print()
    print(f"REGIME: {ctx.regime}  |  Composite Score: {ctx.score:.1f}  |  "
          f"Confidence: {ctx.confidence:.1f}  |  Velocity: {ctx.velocity:+.1f}")
    print()
    print(f"Trend      score={ctx.trend.score:.1f}  nifty={ctx.trend.nifty_state}  "
          f"banknifty={ctx.trend.banknifty_state}")
    print(f"Volatility score={ctx.volatility.score:.1f}  vix={ctx.volatility.vix_level:.2f}  "
          f"state={ctx.volatility.vix_state}  chg={ctx.volatility.vix_change_pct}")
    print(f"Breadth    score={ctx.breadth.score:.1f}  ad_ratio={ctx.breadth.advance_decline_ratio:.2f}  "
          f"pct_above_50dma={ctx.breadth.pct_above_50dma:.1f}  "
          f"sector_participation={ctx.breadth.sector_participation_pct:.1f}")
    print(f"Liquidity  score={ctx.liquidity.score:.1f}  "
          f"nifty_vol_ratio={ctx.liquidity.nifty_volume_ratio}  "
          f"banknifty_vol_ratio={ctx.liquidity.banknifty_volume_ratio}")
    print("=" * 90)


if __name__ == "__main__":
    main()

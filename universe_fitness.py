"""Trading-fitness screener for the scan universe.

Answers a question none of the price/technical scanners ask: is each
universe member even a suitable *vehicle* for short-term trading (BTST /
1-2 week swing) in the first place - independent of whether it has a signal
today?

Guiding principle (user directive, 2026-08-07): the goal is TRADING, not
long-term investment. A stock being a great long-term hold does NOT make
it fit to trade. So unlike a normal quality screen, this one treats range
(ATR%) as a requirement and PENALIZES low volatility - a liquid, blue-chip
name that drifts ~1%/week is flagged TOO_SLOW ("good hold, poor trade"),
because a 1-2 week trade needs enough range to clear costs and risk.

Four trading-first axes, all measured from daily candles (one fetch per
symbol) plus DataBroker's F&O eligibility:
  1. Liquidity  - 20-day average traded value (Rs Cr).
  2. F&O status - overnight safety / OI signal for BTST.
  3. ATR%       - does it actually move enough to trade (the core axis).
  4. Trend      - Minervini-lite Stage 2 participation right now.

Emits per-symbol tags (BTST_ELIGIBLE / SWING_ELIGIBLE / TOO_SLOW /
TOO_ILLIQUID / TOO_HOT) and a 0-100 Trading_Fitness score, and persists the
result to universe_fitness.csv so the scanners can later restrict BTST to
the BTST_ELIGIBLE subset. Plugin-only: no edits to any V1.0 file; all
thresholds live in universe_fitness_config.yaml.
"""
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import pandas as pd
import yaml

import config
from data_broker import DataBroker

logger = logging.getLogger(__name__)

FITNESS_CONFIG_PATH = "universe_fitness_config.yaml"
FITNESS_OUTPUT_PATH = "universe_fitness.csv"

_DEFAULT_CONFIG = {
    'min_turnover_cr_btst': 50.0, 'min_turnover_cr_swing': 20.0,
    'min_atr_pct': 1.8, 'ideal_atr_low': 2.5, 'ideal_atr_high': 6.0, 'max_atr_pct': 9.0,
    'max_pct_below_52w_high': 25.0, 'min_pct_above_52w_low': 30.0,
    'weight_atr': 40, 'weight_liquidity': 25, 'weight_trend': 25, 'weight_fno': 10,
    'history_days': 320,
}


def _load_config() -> dict:
    if not os.path.exists(FITNESS_CONFIG_PATH):
        logger.warning(f"{FITNESS_CONFIG_PATH} not found; using built-in fitness defaults.")
        return _DEFAULT_CONFIG
    try:
        with open(FITNESS_CONFIG_PATH, 'r') as f:
            loaded = yaml.safe_load(f)
        if not isinstance(loaded, dict):
            raise ValueError("top-level YAML content is not a mapping")
        return {**_DEFAULT_CONFIG, **loaded}
    except Exception as e:
        logger.warning(f"Failed to parse {FITNESS_CONFIG_PATH} ({e}); using built-in defaults.")
        return _DEFAULT_CONFIG


def _atr_pct(candles: pd.DataFrame, period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    high, low, close = candles['high'], candles['low'], candles['close']
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    last_close = close.iloc[-1]
    return (atr / last_close) * 100 if last_close else None


def _is_stage2(candles: pd.DataFrame, cfg: dict) -> Optional[bool]:
    """Minervini-lite Stage 2 uptrend participation."""
    if len(candles) < 200:
        return None
    close = candles['close']
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    c = close.iloc[-1]
    hi_52w, lo_52w = close.tail(250).max(), close.tail(250).min()

    above_emas = c > ema50.iloc[-1] > ema200.iloc[-1]
    ema200_rising = ema200.iloc[-1] > ema200.iloc[-20]
    near_high = ((hi_52w - c) / hi_52w) * 100 <= cfg['max_pct_below_52w_high'] if hi_52w else False
    above_low = ((c - lo_52w) / lo_52w) * 100 >= cfg['min_pct_above_52w_low'] if lo_52w else False
    return bool(above_emas and ema200_rising and near_high and above_low)


def _atr_score(atr_pct: Optional[float], cfg: dict) -> float:
    """0-1 range score peaking in the ideal band, penalized below min
    (too slow) and above max (too hot). This is where 'good long-term hold
    but dead chart' gets its low mark."""
    if atr_pct is None:
        return 0.0
    if atr_pct < cfg['min_atr_pct'] or atr_pct > cfg['max_atr_pct']:
        return 0.0
    if cfg['ideal_atr_low'] <= atr_pct <= cfg['ideal_atr_high']:
        return 1.0
    if atr_pct < cfg['ideal_atr_low']:
        return (atr_pct - cfg['min_atr_pct']) / (cfg['ideal_atr_low'] - cfg['min_atr_pct'])
    return (cfg['max_atr_pct'] - atr_pct) / (cfg['max_atr_pct'] - cfg['ideal_atr_high'])


def compute_fitness(symbol: str, candles: pd.DataFrame, fno: bool, cfg: dict) -> dict:
    if candles is None or candles.empty or len(candles) < 200:
        return {'Symbol': symbol, 'Trading_Fitness': None, 'Primary_Tag': 'NO_DATA',
                'Turnover_Cr_20d': None, 'ATR_Pct': None, 'Stage2': None, 'FnO': fno}

    turnover_cr = float((candles['close'] * candles['volume']).tail(20).mean()) / 1e7
    atr_pct = _atr_pct(candles)
    stage2 = _is_stage2(candles, cfg)

    # Component scores (0-1)
    liq_score = min(1.0, turnover_cr / (cfg['min_turnover_cr_btst'] * 2)) if turnover_cr else 0.0
    atr_sc = _atr_score(atr_pct, cfg)
    trend_sc = 1.0 if stage2 else 0.0
    fno_sc = 1.0 if fno else 0.0

    fitness = (
        atr_sc * cfg['weight_atr'] + liq_score * cfg['weight_liquidity']
        + trend_sc * cfg['weight_trend'] + fno_sc * cfg['weight_fno']
    )

    # Tags - a name can be BTST and/or SWING eligible, or carry a disqualifier.
    tags = []
    too_illiquid_swing = turnover_cr < cfg['min_turnover_cr_swing']
    too_slow = atr_pct is not None and atr_pct < cfg['min_atr_pct']
    too_hot = atr_pct is not None and atr_pct > cfg['max_atr_pct']

    if too_slow:
        tags.append('TOO_SLOW')          # good hold, poor trade - the key flag
    if too_hot:
        tags.append('TOO_HOT')
    if too_illiquid_swing:
        tags.append('TOO_ILLIQUID')

    tradeable_range = (atr_pct is not None and cfg['min_atr_pct'] <= atr_pct <= cfg['max_atr_pct'])
    if fno and turnover_cr >= cfg['min_turnover_cr_btst'] and tradeable_range:
        tags.append('BTST_ELIGIBLE')
    if turnover_cr >= cfg['min_turnover_cr_swing'] and tradeable_range and stage2:
        tags.append('SWING_ELIGIBLE')

    # Primary tag = the single most useful label for a reader scanning the list.
    if 'BTST_ELIGIBLE' in tags:
        primary = 'BTST_ELIGIBLE'
    elif 'SWING_ELIGIBLE' in tags:
        primary = 'SWING_ELIGIBLE'
    elif too_slow:
        primary = 'TOO_SLOW'
    elif too_hot:
        primary = 'TOO_HOT'
    elif too_illiquid_swing:
        primary = 'TOO_ILLIQUID'
    else:
        primary = 'MARGINAL'

    return {
        'Symbol': symbol,
        'Trading_Fitness': round(fitness, 1),
        'Primary_Tag': primary,
        'All_Tags': '|'.join(tags) if tags else '',
        'Turnover_Cr_20d': round(turnover_cr, 1),
        'ATR_Pct': round(atr_pct, 2) if atr_pct is not None else None,
        'Stage2': stage2,
        'FnO': fno,
    }


def build_universe_fitness(broker: Optional[DataBroker] = None) -> pd.DataFrame:
    cfg = _load_config()
    owns_broker = broker is None
    broker = broker if broker is not None else DataBroker()
    symbols = list(config.Universe.SECTOR_MAP.keys())

    fno_map = {}
    for sym in symbols:
        try:
            fno_map[sym] = broker.is_fno_eligible(sym)
        except Exception:
            fno_map[sym] = False

    results = []

    def _one(sym):
        candles = broker.fetch_daily_candles(sym, days_back=cfg['history_days'])
        return compute_fitness(sym, candles, fno_map.get(sym, False), cfg)

    with ThreadPoolExecutor(max_workers=config.AppConfig.SAFE_API_WORKERS) as ex:
        futures = {ex.submit(_one, s): s for s in symbols}
        done = 0
        for fut in as_completed(futures):
            done += 1
            print(f"\rFitness: {done}/{len(symbols)}".ljust(30), end="")
            try:
                results.append(fut.result())
            except Exception as e:
                logger.warning(f"Fitness compute failed for {futures[fut]}: {e}")
    print("\r".ljust(30) + "\r", end="")

    if owns_broker:
        broker.close_session()

    df = pd.DataFrame(results)
    df = df.sort_values('Trading_Fitness', ascending=False, na_position='last').reset_index(drop=True)
    try:
        df.to_csv(FITNESS_OUTPUT_PATH, index=False)
        logger.info(f"Universe fitness saved to {FITNESS_OUTPUT_PATH}")
    except OSError as e:
        logger.warning(f"Failed to save {FITNESS_OUTPUT_PATH}: {e}")
    return df


def print_fitness_report(df: pd.DataFrame) -> None:
    if df.empty:
        print("\nNo fitness data.")
        return

    counts = df['Primary_Tag'].value_counts()
    print("\n" + "=" * 100)
    print("UNIVERSE TRADING FITNESS (short-term BTST/swing - NOT long-term quality)")
    print("=" * 100)
    print("Breakdown:", " | ".join(f"{k}: {v}" for k, v in counts.items()))

    cols = ['Symbol', 'Trading_Fitness', 'Turnover_Cr_20d', 'ATR_Pct', 'Stage2', 'FnO', 'All_Tags']

    for tag, label in [
        ('BTST_ELIGIBLE', 'BTST-ELIGIBLE (F&O + liquid + real range)'),
        ('SWING_ELIGIBLE', 'SWING-ELIGIBLE (liquid enough + range + Stage-2 trend)'),
        ('TOO_SLOW', 'TOO SLOW (good long-term hold, poor trade - no range)'),
        ('TOO_ILLIQUID', 'TOO ILLIQUID for short-term trading'),
        ('TOO_HOT', 'TOO HOT (whipsaw/gap risk, esp. overnight)'),
        ('MARGINAL', 'MARGINAL'),
    ]:
        subset = df[df['Primary_Tag'] == tag]
        if subset.empty:
            continue
        print(f"\n--- {label} ({len(subset)}) ---")
        print(subset[cols].to_string(index=False))
    print("=" * 100)
    print(f"Full table saved to {FITNESS_OUTPUT_PATH}. Liquidity=20d avg traded value; "
          f"single-run snapshot - re-run periodically as ranges/trends shift.")

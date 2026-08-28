"""External candidate screener: find better-fitted trading vehicles OUTSIDE
the current universe, and propose add/replace swaps.

universe_fitness.py only looks inward - it flags which current names are
unfit for short-term trading, but has no way to SOURCE better-fitted
replacements. Pruning without sourcing just shrinks the universe. This
closes that loop: it scores every F&O-eligible NSE name NOT already in the
universe using the exact same compute_fitness() (identical criteria - never
forked), then ranks them and pairs the strongest external candidates
against the weakest current holdings.

Candidate pool = the full F&O underlying set (DataBroker.fno_underlyings,
derived from scrip_master.json), minus names already in the universe and
minus exchange test symbols (*NSETEST). F&O-eligibility is itself a
liquidity/tradeability filter, so this is the right pool for BTST/swing
sourcing rather than all of NSE.

IMPORTANT (framing): this produces universe-CONSTRUCTION candidates - names
that are mechanically better-suited *vehicles* for short-term trading. It
is NOT buy/sell advice on any stock. Final inclusion in the scan universe
is the user's call; the fitness score says nothing about whether a name has
a signal today or is worth owning.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd

import config
from data_broker import DataBroker
from universe_fitness import _load_config, compute_fitness, FITNESS_OUTPUT_PATH

logger = logging.getLogger(__name__)

EXPANSION_OUTPUT_PATH = "universe_expansion_candidates.csv"


def _external_fno_candidates(broker: DataBroker) -> list[str]:
    """F&O underlyings not already in the universe, excluding exchange test
    symbols. Sorted for deterministic output."""
    current = set(config.Universe.SECTOR_MAP.keys())
    candidates = []
    for sym in broker.fno_underlyings:
        if sym in current:
            continue
        if 'NSETEST' in sym or sym.endswith('TEST'):
            continue
        candidates.append(sym)
    return sorted(candidates)


def build_expansion_candidates(broker: Optional[DataBroker] = None) -> pd.DataFrame:
    cfg = _load_config()
    owns_broker = broker is None
    broker = broker if broker is not None else DataBroker()

    candidates = _external_fno_candidates(broker)
    logger.info(f"Scoring {len(candidates)} external F&O candidates for trading fitness...")

    results = []

    def _one(sym):
        candles = broker.fetch_daily_candles(sym, days_back=cfg['history_days'])
        # Every candidate here is F&O-eligible by construction.
        return compute_fitness(sym, candles, fno=True, cfg=cfg)

    with ThreadPoolExecutor(max_workers=config.AppConfig.SAFE_API_WORKERS) as ex:
        futures = {ex.submit(_one, s): s for s in candidates}
        done = 0
        for fut in as_completed(futures):
            done += 1
            print(f"\rExpansion: {done}/{len(candidates)}".ljust(30), end="")
            try:
                results.append(fut.result())
            except Exception as e:
                logger.warning(f"Expansion compute failed for {futures[fut]}: {e}")
    print("\r".ljust(30) + "\r", end="")

    if owns_broker:
        broker.close_session()

    df = pd.DataFrame(results)
    if df.empty:
        return df
    df = df.sort_values('Trading_Fitness', ascending=False, na_position='last').reset_index(drop=True)
    try:
        df.to_csv(EXPANSION_OUTPUT_PATH, index=False)
        logger.info(f"External candidates saved to {EXPANSION_OUTPUT_PATH}")
    except OSError as e:
        logger.warning(f"Failed to save {EXPANSION_OUTPUT_PATH}: {e}")
    return df


def swap_analysis(candidates_df: pd.DataFrame, top_n: int = 15) -> dict:
    """Pairs the strongest external candidates against the weakest current
    holdings (read from universe_fitness.csv, produced by
    `python main.py fitness`). Returns dict with 'add_candidates' (external
    names that clear BTST/SWING eligibility) and 'replace_targets' (current
    names that don't), so a swap keeps the universe roughly constant-size."""
    add_candidates = candidates_df[
        candidates_df['Primary_Tag'].isin(['BTST_ELIGIBLE', 'SWING_ELIGIBLE'])
    ].head(top_n)

    replace_targets = pd.DataFrame()
    try:
        current = pd.read_csv(FITNESS_OUTPUT_PATH)
        replace_targets = current[
            current['Primary_Tag'].isin(['TOO_SLOW', 'TOO_ILLIQUID', 'TOO_HOT'])
        ].sort_values('Trading_Fitness', na_position='first').head(top_n)
    except FileNotFoundError:
        logger.warning(f"{FITNESS_OUTPUT_PATH} not found - run `python main.py fitness` first for swap targets.")

    return {'add_candidates': add_candidates, 'replace_targets': replace_targets}


def print_expansion_report(candidates_df: pd.DataFrame) -> None:
    if candidates_df.empty:
        print("\nNo external candidates scored.")
        return

    cols = ['Symbol', 'Trading_Fitness', 'Turnover_Cr_20d', 'ATR_Pct', 'Stage2', 'Primary_Tag']
    swap = swap_analysis(candidates_df)

    print("\n" + "=" * 100)
    print("UNIVERSE EXPANSION - external F&O names scored on the SAME trading-fitness criteria")
    print("=" * 100)

    add = swap['add_candidates']
    print(f"\n--- TOP ADD CANDIDATES (external, BTST/SWING-eligible) ({len(add)}) ---")
    print(add[cols].to_string(index=False) if not add.empty else "  (none cleared eligibility)")

    repl = swap['replace_targets']
    if not repl.empty:
        print(f"\n--- CURRENT NAMES THESE COULD REPLACE (unfit for short-term trading) ({len(repl)}) ---")
        print(repl[['Symbol', 'Trading_Fitness', 'Turnover_Cr_20d', 'ATR_Pct', 'Primary_Tag']].to_string(index=False))
        print("\nSwap logic: retire the bottom current names above, promote the top external candidates - "
              "keeps the universe size roughly constant while raising average trading fitness.")
    else:
        print("\n(Run `python main.py fitness` first to generate current-universe scores for swap targets.)")

    print("=" * 100)
    print("NOTE: These are universe-construction candidates - mechanically better-fitted trading VEHICLES, "
          "not buy recommendations. Final inclusion is your call.")

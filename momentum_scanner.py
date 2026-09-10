"""Always-on momentum/breakout scan - the non-gated sibling of emfb.py.

EMFB (emfb.py) only produces a report when its market-weakness regime gate
activates (index returns below threshold, weak breadth, or elevated VIX -
see config.EMFB_Regime). That's correct for EMFB's own purpose, but it means
a genuine early breakout can sit unreported for days if the broader market
stays calm in between (e.g. APARINDS first crossed EMFB's own score
threshold on 2026-07-31, then the market stayed quiet enough that EMFB
didn't fire again until 2026-08-04 - three sessions of the move went by
with no fresh report).

This module reuses EMFB's actual scoring engine unmodified
(scanner_engine.EMFBScanner.compute_emfb_metrics / rank_and_score_emfb) and
its universe/data-fetch helpers (emfb.py's _fetch_data_for_universe etc.) -
nothing about the metrics or scoring is forked or reimplemented. The only
difference is dropping the regime gate: this scan runs every time it's
invoked within its own trading-hours window, producing its own report
files/cache/DB table so it never collides with EMFB's.
"""
import logging
import os
import pickle
import time as time_sleep
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yaml

import config
from data_broker import DataBroker
from database import SignalDB
from scanner_engine import EMFBScanner
from provider import ConstituentProvider
from lifecycle_manager import shutdown_manager
from emfb import CORE_INDICES, _fetch_data_for_universe
from earnings_verifier import apply_nse_earnings_fallback
from nse_daily_history import NSEDailyHistory

logger = logging.getLogger(__name__)

COVERAGE_WARN_PCT = 90.0  # below this share of the universe scored, warn loudly

MOMENTUM_CONFIG_PATH = "momentum_config.yaml"
MOMENTUM_CACHE_PATH = "discovery_cache_momentum.pkl"

_DEFAULT_MOMENTUM_CONFIG = {
    'scan_window': {'start': '09:15', 'end': '18:00'},
    'weight_profile': 'DEFAULT',
    'report_top_n': 20,
    'cache_ttl_minutes': 60,
}


def _load_momentum_config() -> dict:
    """Loads momentum_config.yaml, falling back to built-in defaults on any
    missing/malformed file - mirrors final_ranker.py's RankerConfig fallback
    behavior so this plugin never crashes on a bad/missing config."""
    if not os.path.exists(MOMENTUM_CONFIG_PATH):
        logger.warning(f"{MOMENTUM_CONFIG_PATH} not found; using built-in momentum scan defaults.")
        return _DEFAULT_MOMENTUM_CONFIG
    try:
        with open(MOMENTUM_CONFIG_PATH, 'r') as f:
            loaded = yaml.safe_load(f)
        if not isinstance(loaded, dict):
            raise ValueError("top-level YAML content is not a mapping")
        merged = {**_DEFAULT_MOMENTUM_CONFIG, **loaded}
        return merged
    except Exception as e:
        logger.warning(f"Failed to parse {MOMENTUM_CONFIG_PATH} ({e}); using built-in momentum scan defaults.")
        return _DEFAULT_MOMENTUM_CONFIG


def _is_scan_window(cfg: dict) -> bool:
    now = datetime.now(config.MARKET_TZ)
    if now.weekday() >= 5:
        logger.info("Market is closed (weekend). Skipping momentum scan.")
        return False
    window = cfg.get('scan_window', _DEFAULT_MOMENTUM_CONFIG['scan_window'])
    window_start = time.fromisoformat(window['start'])
    window_end = time.fromisoformat(window['end'])
    if not (window_start <= now.time() <= window_end):
        logger.info(
            f"Current time {now.time().strftime('%H:%M:%S')} is outside the momentum scan window "
            f"({window['start']}-{window['end']})."
        )
        return False
    return True


# Last-hour volume / closing-strength factors only become meaningful once the
# final hour is underway; see _load_momentum_cache for the measured evidence.
CLOSING_FACTORS_VALID_FROM = time(14, 30)


def _load_momentum_cache(cfg: dict) -> Optional[pd.DataFrame]:
    if not os.path.exists(MOMENTUM_CACHE_PATH):
        return None
    try:
        with open(MOMENTUM_CACHE_PATH, 'rb') as f:
            payload = pickle.load(f)
    except Exception as e:
        logger.warning(f"Failed to load momentum cache: {e}")
        return None

    created_at_str = payload.get('created_at') if isinstance(payload, dict) else None
    if not created_at_str:
        return None
    try:
        created_at = datetime.fromisoformat(created_at_str)
    except ValueError:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=config.MARKET_TZ)

    now = datetime.now(config.MARKET_TZ)
    if created_at.date() != now.date():
        return None
    ttl_minutes = cfg.get('cache_ttl_minutes', _DEFAULT_MOMENTUM_CONFIG['cache_ttl_minutes'])
    if now - created_at > timedelta(minutes=ttl_minutes):
        return None

    # A scan taken before 14:30 and one taken after are not interchangeable, however
    # recent the earlier one is. Last_Hour_Vol and the closing-strength factors are
    # timing-gated: a 13:09 scan produced 11 unique Last_Hour_Vol values across 132
    # rows (122 sharing one fallback) where a 15:28 scan produced 129 across 133.
    # Serving the pre-14:30 scan across that boundary silently answers an afternoon
    # question with morning data. Measured live 2026-09-10: `main.py momentum` at
    # 14:35 reused Prewarm's 14:10 scan; the genuine 15:11 re-scan moved COCHINSHIP
    # 76.2 -> 65.9 (out of the shortlist) and TAALTECH 86.4 -> 91.7. Same universe,
    # same day, one hour apart.
    if created_at.time() < CLOSING_FACTORS_VALID_FROM <= now.time():
        logger.warning(
            f"Momentum cache from {created_at.strftime('%H:%M:%S')} predates the "
            f"{CLOSING_FACTORS_VALID_FROM.strftime('%H:%M')} closing-factors boundary and it is "
            f"now {now.strftime('%H:%M:%S')} - discarding it and re-scanning, because "
            f"last-hour volume and closing strength are not comparable across that line."
        )
        return None

    df = payload.get('df')
    if not isinstance(df, pd.DataFrame):
        return None
    # Loud on purpose: this is a full no-op masquerading as a scan. It was missed live
    # on 2026-09-10 because it was a single INFO line in a busy log.
    msg = (f"Momentum cache HIT - reusing the {created_at.strftime('%H:%M:%S')} scan "
           f"({len(df)} ranked symbols). NO new data was fetched. "
           f"Pass --force-refresh to actually re-scan.")
    logger.warning(msg)
    print(f"\n[!] {msg}\n")
    return df


def _save_momentum_cache(df: pd.DataFrame) -> None:
    payload = {'created_at': datetime.now(config.MARKET_TZ).isoformat(timespec='seconds'), 'df': df}
    try:
        with open(MOMENTUM_CACHE_PATH, 'wb') as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    except OSError as e:
        logger.warning(f"Failed to save momentum cache: {e}")


def _generate_report(df: pd.DataFrame, top_n: int):
    if df.empty:
        print("\nNo momentum signals found.")
        return

    df = df.sort_values(by='EMFB_Score', ascending=False).reset_index(drop=True)
    df['Rank'] = df.index + 1

    print("\n" + "=" * 120)
    print("ALWAYS-ON MOMENTUM / BREAKOUT SCAN (non-gated, runs every session)")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Universe coverage. Rate limiting silently drops symbols during the fetch
    # stage, so a run can score a fraction of the universe and still print a
    # clean-looking top-20 with no indication anything is missing. Observed
    # 2026-09-01: 123 of 215 scored (43% invisible), and the largest 20-day
    # mover in the whole universe (KALYANKJIL, +35%) was among the dropped -
    # 89 of the 92 missing symbols had perfectly good cached data, so this is
    # dropped coverage, not a data gap. Absence has to be visible or it reads
    # as "nothing there".
    coverage = df.attrs.get('coverage')
    if coverage:
        scanned, expected = coverage['scanned'], coverage['expected']
        pct = (scanned / expected * 100) if expected else 0.0
        line = f"Universe coverage: {scanned}/{expected} symbols ({pct:.0f}%)"
        if pct < COVERAGE_WARN_PCT:
            print(f"⚠️  {line} - {expected - scanned} NOT SCORED this run.")
            print("⚠️  Names absent below are NOT necessarily weak - they may never have been")
            print("⚠️  scored. Re-run, or cross-check with `python main.py grind` (reads cache).")
        else:
            print(line)
    print("=" * 120)

    display_cols = [
        'Rank', 'Symbol', 'EMFB_Score', 'Confidence', 'RS_vs_Nifty', 'RS_vs_Sector',
        'Recovery', 'Closing', 'VWAP_Score', 'Last_Hour_Vol', 'Breakout_Score', 'Reason',
        'Trigger', 'Stop', 'Target', 'Data_Stale'
    ]
    report_df = df[[c for c in display_cols if c in df.columns]].copy()
    print(report_df.head(top_n).to_string(index=False))
    print("=" * 120)

    try:
        from sector_rotation import print_rs_only_rotation_report
        print_rs_only_rotation_report(df)
    except Exception as e:
        logger.warning(f"Sector rotation section skipped due to an error: {e}", exc_info=True)

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_filename = f"momentum_report_{timestamp_str}"
    try:
        df.to_csv(f"{base_filename}.csv", index=False)
        df.to_json(f"{base_filename}.json", orient='records', lines=True)
        df.to_parquet(f"{base_filename}.parquet", index=False)

        db = SignalDB(db_path='reports.db')
        db.log_dataframe(df, table_name='momentum_signals')

        logger.info(f"Momentum reports successfully saved to {base_filename}.[csv, json, parquet] and reports.db")
    except Exception as e:
        # Also print (not just log) - see the matching note in emfb.py's
        # _generate_report: a silent reports.db write failure there went
        # unnoticed for days because logger.error alone isn't guaranteed
        # visible in every run context.
        print(f"⚠️ Failed to save momentum reports: {e}")
        logger.error(f"Failed to save momentum reports: {e}", exc_info=True)


MIN_DAILY_SESSIONS = 50  # matches BaseScanner.compute_daily_metrics' own floor
BHAVCOPY_TRADING_DAYS = 290  # covers Pivot_250's 250-session window with buffer


def _fetch_universe_daily_via_bhavcopy(symbols: list, data_store: dict) -> dict:
    """Fills data_store[symbol]['daily'] from NSE bhavcopy instead of the
    broker - see nse_daily_history.py. One-time backfill cost (~290
    sequential day-files, cached to disk forever after) replaces what
    would otherwise be ~len(symbols) throttled broker calls every run.

    Fail-soft per symbol: bhavcopy can lack recent listings, symbols with
    NSE/broker naming mismatches, etc. - anything under MIN_DAILY_SESSIONS
    (or missing entirely) falls back to broker.fetch_ohlcv for just that
    symbol, so momentum's coverage never regresses versus the old
    all-broker path, only its API load does.
    """
    history_feed = NSEDailyHistory()
    try:
        bulk = history_feed.fetch_daily_history_bulk(symbols, days_back=BHAVCOPY_TRADING_DAYS)
    except Exception as e:
        logger.warning(f"NSE bhavcopy bulk fetch failed entirely ({e}); all symbols fall back to broker.")
        bulk = {}
    finally:
        history_feed.close()

    fallback_needed = [s for s in symbols if len(bulk.get(s, pd.DataFrame())) < MIN_DAILY_SESSIONS]
    for sym in symbols:
        if sym not in fallback_needed:
            data_store.setdefault(sym, {})['daily'] = bulk[sym]

    if fallback_needed:
        logger.info(
            f"Bhavcopy covered {len(symbols) - len(fallback_needed)}/{len(symbols)} symbols; "
            f"{len(fallback_needed)} falling back to broker (new listings/naming gaps)."
        )
    return fallback_needed


def run_momentum_scan(broker: Optional[DataBroker] = None, force_refresh: bool = False) -> pd.DataFrame:
    """Always-on counterpart to emfb.run_emfb_scan: same scoring engine, no
    regime gate. Returns the ranked DataFrame (empty if outside the scan
    window or nothing cleared the score threshold)."""
    cfg = _load_momentum_config()

    if not force_refresh:
        cached = _load_momentum_cache(cfg)
        if cached is not None:
            return cached

    if not _is_scan_window(cfg):
        return pd.DataFrame()

    start_time = time_sleep.time()
    broker = broker if broker is not None else DataBroker()
    scanner = EMFBScanner()

    provider = ConstituentProvider(index_name=config.EMFB.UNIVERSE_INDEX)
    universe = provider.get_universe()
    symbols_only = [s['symbol'] for s in universe]

    # Unlike run_emfb_scan's staged fetch (cheap index precheck, then bail if
    # the regime gate doesn't activate), this scanner always needs the full
    # dataset since it always scores - so fetch everything up front.
    data_store = _fetch_data_for_universe(broker, CORE_INDICES, ['daily'])
    if not data_store:
        logger.error("Failed to fetch index data. Aborting momentum scan.")
        return pd.DataFrame()

    nifty_df = data_store.get('Nifty 50', {}).get('daily')
    if nifty_df is None or nifty_df.empty:
        logger.error("Nifty 50 daily data unavailable. Aborting momentum scan.")
        return pd.DataFrame()

    # Universe daily candles: bhavcopy first (one HTTP call per trading day
    # covers the whole market, disk-cached forever), broker only for the
    # residual gap - see _fetch_universe_daily_via_bhavcopy. Cuts momentum's
    # Angel API load by ~len(symbols_only) calls per run (this is what was
    # driving the 2026-08-10 rate-limit incident on the broker-only path).
    fallback_symbols = _fetch_universe_daily_via_bhavcopy(symbols_only, data_store)
    if fallback_symbols:
        data_store = _fetch_data_for_universe(broker, fallback_symbols, ['daily'], data_store)

    sector_indices_to_fetch = set()
    for stock_info in universe:
        sector_index = config.Universe.SECTOR_INDEX_MAP.get(stock_info.get('sector', 'OTHER'))
        if sector_index:
            sector_indices_to_fetch.add(sector_index)
    data_store = _fetch_data_for_universe(broker, sorted(sector_indices_to_fetch), ['daily'], data_store)
    data_store = _fetch_data_for_universe(broker, symbols_only, ['hourly', '15min', '5min'], data_store)

    weight_profile = cfg.get('weight_profile', 'DEFAULT')
    logger.info(f"Applying momentum weight profile: {weight_profile}")

    raw_metrics = []
    scan_start_time = time_sleep.time()

    def compute_metrics_for_stock(stock_info):
        if shutdown_manager.is_shutdown():
            return None
        stock_symbol = stock_info['symbol']
        stock_data_all_tf = {
            'daily': data_store.get(stock_symbol, {}).get('daily'),
            'hourly': data_store.get(stock_symbol, {}).get('hourly'),
            '15min': data_store.get(stock_symbol, {}).get('15min'),
            '5min': data_store.get(stock_symbol, {}).get('5min'),
            'sector': stock_info.get('sector', 'OTHER')
        }
        stock_sector = stock_info.get('sector', 'OTHER')
        sector_index_name = config.Universe.SECTOR_INDEX_MAP.get(stock_sector)
        sector_df = data_store.get(sector_index_name, {}).get('daily') if sector_index_name else None

        is_stale = any(
            df is not None and df.attrs.get('stale')
            for df in (stock_data_all_tf.get('hourly'), stock_data_all_tf.get('15min'), stock_data_all_tf.get('5min'))
        )

        try:
            metrics = scanner.compute_emfb_metrics(stock_symbol, stock_data_all_tf, nifty_df, sector_df)
            if metrics is not None:
                metrics['Data_Stale'] = is_stale
            return metrics
        except Exception as e:
            logger.error(f"Momentum metric calculation error for {stock_symbol}: {e}", exc_info=False)
            return None

    with ThreadPoolExecutor(max_workers=config.AppConfig.SAFE_API_WORKERS) as executor:
        stocks_to_scan = [s for s in universe if s['symbol'] not in CORE_INDICES]
        total_to_scan = len(stocks_to_scan)
        futures = {executor.submit(compute_metrics_for_stock, stock_info): stock_info['symbol'] for stock_info in stocks_to_scan}

        for i, future in enumerate(as_completed(futures)):
            if shutdown_manager.is_shutdown():
                for f in futures:
                    f.cancel()
                break

            elapsed = time_sleep.time() - scan_start_time
            eta = (elapsed / (i + 1)) * (total_to_scan - (i + 1)) if i > 0 else 0
            progress = f"Momentum Stage 1: Computing Metrics | {(i+1)/total_to_scan:.1%} | {i+1}/{total_to_scan} | ETA: {eta:.0f}s"
            print(f"\r{progress.ljust(80)}", end="")

            metric_result = future.result()
            if metric_result:
                raw_metrics.append(metric_result)

    scan_duration = time_sleep.time() - scan_start_time
    print(f"\r{' ' * 80}\r", end="")
    logger.info(f"Momentum Stage 1 (Metrics) complete in {scan_duration:.2f}s. Processed {len(raw_metrics)} stocks.")

    if not raw_metrics:
        logger.info("No stocks passed initial metric calculation.")
        return pd.DataFrame()

    # Stage 1.5: one bounded retry pass for symbols that failed to produce a
    # usable metric (usually AB1021 rate-limit rejections during Stage 1's
    # fetch). Added 2026-09-07 after a live run silently dropped 76/215
    # symbols to rate limiting with zero automatic recovery - see
    # project_coverage_check_gap_20260907 / project_rs_blindness_fix_20260907
    # memory for the incident this was built from. ONE retry only, not a
    # loop: AB1021 is a broker-side rate limit (documented as inconsistent
    # even at well-under-budget traffic, see project_angel_api_rate_limit
    # memory), not guaranteed to clear on a second attempt, and looping here
    # risks turning an already-long scan into a much longer one for
    # uncertain gain. A short pause before retrying gives the limiter a beat
    # rather than immediately re-hitting the same burst window.
    succeeded_symbols = {
        m['Symbol'] for m in raw_metrics
        if m.get('close') is not None and not pd.isna(m.get('close'))
    }
    missing_stocks = [s for s in stocks_to_scan if s['symbol'] not in succeeded_symbols]
    if missing_stocks:
        missing_symbols = [s['symbol'] for s in missing_stocks]
        logger.info(
            f"Momentum Stage 1.5: {len(missing_symbols)} symbol(s) missing a usable metric "
            f"after Stage 1 - retrying once after a short pause: "
            f"{missing_symbols[:10]}{'...' if len(missing_symbols) > 10 else ''}"
        )
        time_sleep.sleep(5)
        data_store = _fetch_data_for_universe(broker, missing_symbols, ['hourly', '15min', '5min'], data_store)
        recovered = 0
        with ThreadPoolExecutor(max_workers=config.AppConfig.SAFE_API_WORKERS) as executor:
            retry_futures = {executor.submit(compute_metrics_for_stock, s): s['symbol'] for s in missing_stocks}
            for future in as_completed(retry_futures):
                metric_result = future.result()
                if metric_result and metric_result.get('close') is not None and not pd.isna(metric_result.get('close')):
                    raw_metrics.append(metric_result)
                    recovered += 1
        logger.info(
            f"Momentum Stage 1.5 retry complete: {recovered}/{len(missing_symbols)} recovered, "
            f"{len(missing_symbols) - recovered} still missing after retry."
        )

    logger.info("Momentum Stage 2: Ranking universe and calculating final scores...")
    metrics_df = pd.DataFrame(raw_metrics).replace([np.inf, -np.inf], np.nan).dropna(subset=['close'])

    # Coverage is measured against the universe we set out to scan, not against
    # what survived - the whole point is to make silent attrition visible.
    # Checked AFTER the dropna above (not against len(raw_metrics) before it):
    # a symbol can produce a Stage 1 row that's just NaN placeholders from a
    # failed fetch, inflating the pre-dropna count while the row never
    # actually makes it into the report. Found 2026-09-07: a run logged
    # "215/215 scored" here while the saved report had only 139 rows, because
    # this check was reading len(raw_metrics) instead of the post-dropna count.
    expected_universe = len(symbols_only)
    scored_count = len(metrics_df)
    if expected_universe and (scored_count / expected_universe * 100) < COVERAGE_WARN_PCT:
        logger.warning(
            f"Universe coverage {scored_count}/{expected_universe} "
            f"({scored_count / expected_universe * 100:.0f}%) - "
            f"{expected_universe - scored_count} symbols were NOT scored this run "
            f"(usually API rate limiting during the fetch stage, not missing data)."
        )

    final_df = scanner.rank_and_score_emfb(metrics_df, weight_profile)
    final_df = apply_nse_earnings_fallback(final_df)

    # Informational-only Institutional_Score column (real NSE bhavcopy
    # delivery data) - same convention as discovery.py's BTST/SWING output
    # (see institutional_flow.compute_institutional_scores docstring for
    # the validation history). NEVER used in ranking/sorting/filtering here
    # either; NaN-safe, wrapped so a failure just leaves the column NaN.
    try:
        from institutional_flow import compute_institutional_scores
        daily_by_symbol = {
            sym: data_store.get(sym, {}).get('daily')
            for sym in final_df['Symbol']
        }
        scores = compute_institutional_scores(daily_by_symbol)
        final_df['Institutional_Score'] = final_df['Symbol'].map(scores)
    except Exception as e:
        logger.warning(f"Institutional_Score column skipped due to an error: {e}", exc_info=True)
        final_df['Institutional_Score'] = float('nan')

    # Informational-only Grind_Flag column - closes the "RS is a 1-day
    # measure" blind spot (see grind_scanner.py's module docstring: a stock
    # compounding ~1%/day for weeks never prints a big 1-day RS, so it stays
    # invisible to this ranking no matter how strong the underlying trend
    # is - the PTCIL/SYRMA case). grind_scanner was built 2026-08-28 to
    # compensate but was only ever a separate command a human had to
    # remember to run, so its signal never reached this report. Wired in
    # 2026-09-07. Zero extra API calls (reads the same daily cache this scan
    # already populated); NEVER used to rank/filter/reorder here either -
    # same informational-only convention as Institutional_Score/Reliability_Flag.
    # Move_Stage sits alongside Grind_Flag for the same reason and same cost
    # (zero extra API calls, same grind_table build) - see
    # grind_scanner.annotate_move_stage()'s docstring for why this exists:
    # Grind_Flag says WHETHER a name is moving, Move_Stage says WHERE in that
    # move it is (EARLY/MIDWAY/EXTENDED/STALLING) - built 2026-09-08 after a
    # session where every High-confidence name but two turned out to already
    # be extended, and telling the two apart required a human manually
    # cross-referencing today's RS against Ret20d_Pct by hand.
    try:
        from grind_scanner import build_grind_table, annotate_grind, annotate_move_stage
        grind_table = build_grind_table()  # no momentum_report_path - RS_1D set from final_df below instead, since it's more current than any file on disk
        if not grind_table.empty:
            rs_lookup = final_df.set_index('Symbol')['RS_vs_Nifty']
            grind_table['RS_1D'] = grind_table['Symbol'].map(rs_lookup)
            grind_table['In_Momentum_Scan'] = grind_table['Symbol'].isin(final_df['Symbol'])
            grind_table = annotate_move_stage(annotate_grind(grind_table))
            grind_cols = grind_table[['Symbol', 'Grind_Flag', 'Ret20d_Pct', 'Move_Stage']].rename(
                columns={'Ret20d_Pct': 'Grind_Ret20d_Pct'}
            )
            # Re-running the annotation over a frame that already carries these
            # columns must overwrite them, not produce _x/_y pairs - a suffixed
            # merge leaves the plain name missing and downstream stage filters
            # then read a column that isn't there (see decision_brief 82738f9).
            dupes = [c for c in grind_cols.columns if c != 'Symbol' and c in final_df.columns]
            if dupes:
                final_df = final_df.drop(columns=dupes)
            final_df = final_df.merge(grind_cols, on='Symbol', how='left')
        else:
            final_df['Grind_Flag'] = ''
            final_df['Grind_Ret20d_Pct'] = float('nan')
            final_df['Move_Stage'] = ''
    except Exception as e:
        logger.warning(f"Grind_Flag/Move_Stage column skipped due to an error: {e}", exc_info=True)
        final_df['Grind_Flag'] = ''
        final_df['Grind_Ret20d_Pct'] = float('nan')
        final_df['Move_Stage'] = ''

    top_n = cfg.get('report_top_n', 20)
    # Set immediately before the report rather than at creation: several
    # transforms above return new frames, and pandas .attrs does not survive
    # every one of them.
    final_df.attrs['coverage'] = {'scanned': scored_count, 'expected': expected_universe}

    _generate_report(final_df, top_n)

    total_duration = time_sleep.time() - start_time
    logger.info(f"Momentum scan finished in {total_duration:.2f} seconds.")
    _save_momentum_cache(final_df)
    return final_df

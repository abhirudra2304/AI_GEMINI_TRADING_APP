import logging
import os
import pickle
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
from typing import Optional, Dict
import time as time_sleep

import config
from data_broker import DataBroker
from database import SignalDB
from scanner_engine import EMFBScanner
from provider import ConstituentProvider
from lifecycle_manager import shutdown_manager
from earnings_verifier import apply_nse_earnings_fallback

logger = logging.getLogger(__name__)


CORE_INDICES = ['Nifty 50', 'NIFTY BANK', 'NIFTY MIDCAP 100', 'NIFTY SMALLCAP 100', 'India VIX']

# Result-level cache for the whole EMFB run, separate from fetch_ohlcv's raw
# candle cache. Without this, `python main.py eod` running EMFB as its third
# stage right after a standalone `python main.py emfb` re-fetches almost
# everything from scratch: 3 of EMFB's 4 timeframes (hourly/15min/5min) use
# fetch_ohlcv's 50-second cache TTL (tuned for LiveScanner's 60s loop), which
# has always expired again by the time a second full-universe scan+fetch
# finishes. This reuses the same TTL knob Discovery already uses for BTST/SWING.
EMFB_CACHE_PATH = "discovery_cache_emfb.pkl"


def _load_emfb_cache() -> Optional[pd.DataFrame]:
    if not os.path.exists(EMFB_CACHE_PATH):
        return None
    try:
        with open(EMFB_CACHE_PATH, 'rb') as f:
            payload = pickle.load(f)
    except Exception as e:
        logger.warning(f"Failed to load EMFB cache: {e}")
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
    if now - created_at > timedelta(minutes=config.CacheConfig.CACHE_REFRESH_MINUTES):
        return None

    df = payload.get('df')
    if not isinstance(df, pd.DataFrame):
        return None
    logger.info(f"⚡ EMFB cache hit: reusing scan from {created_at.strftime('%H:%M:%S')} ({len(df)} ranked symbols).")
    return df


def _save_emfb_cache(df: pd.DataFrame) -> None:
    payload = {'created_at': datetime.now(config.MARKET_TZ).isoformat(timespec='seconds'), 'df': df}
    try:
        with open(EMFB_CACHE_PATH, 'wb') as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    except OSError as e:
        logger.warning(f"Failed to save EMFB cache: {e}")

# Timeframe name -> (broker interval, days_back). The 15m/5m frames intentionally
# span multiple days: the scanner slices out the latest session and uses the
# prior sessions as the last-hour volume baseline.
TIMEFRAME_FETCH_SPECS = {
    # 400 (not 250) matches discovery.py's Discovery-phase daily window exactly,
    # so EMFB and Discovery share ONE cache file per symbol (data_broker.py keys
    # the cache by days_back, so a mismatched window means two independently-
    # maintained caches for the same underlying daily candles). It also covers
    # compute_daily_metrics's Pivot_250 (rolling(250) - needs 251+ trading days;
    # 250 calendar days is only ~170-175 trading days and was silently under it).
    'daily': ('ONE_DAY', 400),
    'hourly': ('ONE_HOUR', 100),
    '15min': ('FIFTEEN_MINUTE', 80),
    '5min': ('FIVE_MINUTE', 80),
}


def _is_scan_window() -> bool:
    """EMFB is an end-of-day setup: run it from 14:30 (last hour) until 18:00 on weekdays.

    Running earlier is pointless because the last-hour and closing-strength factors
    are not observable yet; running after close (up to 18:00) is fine — the scan
    prepares next-day BTST/swing entries from the completed session.
    """
    now = datetime.now(config.MARKET_TZ)
    if now.weekday() >= 5:
        logger.info("Market is closed (weekend). Skipping EMFB scan.")
        return False
    window_start = time(14, 30)
    window_end = time(18, 0)
    if not (window_start <= now.time() <= window_end):
        logger.info(
            f"Current time {now.time().strftime('%H:%M:%S')} is outside the EMFB window "
            f"(14:30-18:00). Last-hour and closing factors are only meaningful near the close."
        )
        return False
    return True


def _check_index_regime(data_store: dict) -> tuple[bool, list]:
    """Checks index returns and VIX for market weakness. Cheap: needs index data only."""
    reasons = []
    activated = False

    index_thresholds = {
        "Nifty 50": config.EMFB_Regime.NIFTY_RETURN_THRESHOLD,
        "NIFTY BANK": config.EMFB_Regime.BANKNIFTY_RETURN_THRESHOLD,
        "NIFTY MIDCAP 100": config.EMFB_Regime.MIDCAP_RETURN_THRESHOLD,
        "NIFTY SMALLCAP 100": config.EMFB_Regime.SMALLCAP_RETURN_THRESHOLD,
    }
    for index, threshold in index_thresholds.items():
        df = data_store.get(index, {}).get('daily')
        if df is not None and len(df) > 1:
            ret = (df['Close'].iloc[-1] / df['Close'].iloc[-2] - 1) * 100
            if ret < threshold:
                activated = True
                reasons.append(f"{index.replace('NIFTY ', '')} ({ret:.2f}%) < {threshold}%")

    vix_df = data_store.get('India VIX', {}).get('daily')
    if vix_df is not None and not vix_df.empty:
        vix_level = vix_df['Close'].iloc[-1]
        if vix_level > config.EMFB_Regime.VIX_THRESHOLD:
            activated = True
            reasons.append(f"VIX ({vix_level:.2f}) > {config.EMFB_Regime.VIX_THRESHOLD}")

    return activated, reasons


def _get_market_regime_label(reasons: list) -> str:
    """Determines a simple market regime label for weight selection."""
    if any("VIX" in r for r in reasons) or any("Smallcap" in r for r in reasons):
        return 'BEARISH_MARKET'
    if reasons:
        return 'NEUTRAL_MARKET'
    return 'DEFAULT' # Should not happen if scan is activated, but a safe fallback.


def _fetch_data_for_stock(broker: DataBroker, stock: str, timeframes: list) -> Optional[Dict[str, pd.DataFrame]]:
    """Fetches only the requested timeframes for a single stock.

    `timeframes` used to be silently ignored here - this function fetched all
    four TIMEFRAME_FETCH_SPECS entries every call regardless of what the caller
    asked for, which meant _fetch_data_for_universe's ['daily']-only "cheap
    pre-check" stages (see run_emfb_scan) were actually fetching all four
    timeframes for the whole universe before the regime gate had even decided
    whether to run the real scan. Confirmed via logs: a stage logged as
    "Fetching ['daily'] data for 220 symbols" took 719s - about 4x what a
    genuinely daily-only fetch costs at this app's ~1.1s/call rate limit.
    """
    if shutdown_manager.is_shutdown(): return None
    frames = {}
    for tf in timeframes:
        interval, days_back = TIMEFRAME_FETCH_SPECS[tf]
        frames[tf] = broker.fetch_ohlcv(stock, interval, days_back, caller="EMFB_Fetch")
    return frames if any(df is not None and not df.empty for df in frames.values()) else None

def _check_breadth_regime(data_store: dict) -> tuple[bool, list]:
    """Checks the universe advance/decline ratio. Needs universe daily data."""
    advances, declines = 0, 0
    for symbol, stock_data in data_store.items():
        if symbol in CORE_INDICES:
            continue
        df = stock_data.get('daily')
        if df is not None and len(df) > 1:
            if df['Close'].iloc[-1] > df['Close'].iloc[-2]:
                advances += 1
            else:
                declines += 1
    ad_ratio = advances / declines if declines > 0 else float('inf')
    if ad_ratio < config.EMFB_Regime.AD_RATIO_THRESHOLD:
        return True, [f"A/D Ratio ({ad_ratio:.2f}) < {config.EMFB_Regime.AD_RATIO_THRESHOLD}"]
    return False, []


def _fetch_data_for_universe(broker: DataBroker, symbols: list, timeframes: list, data_store: dict = None) -> dict:
    """Fetches the requested timeframes for the given symbols in parallel.

    Results are merged into (and returned as) `data_store`, so the caller can
    stage fetches: indices first, universe dailies second, intraday only once
    the regime gate has actually activated the scan.
    """
    data_store = data_store if data_store is not None else {}
    symbols = list(dict.fromkeys(symbols))
    logger.info(f"Fetching {timeframes} data for {len(symbols)} symbols...")
    total = len(symbols)
    start_time = time_sleep.time()

    def fetch_stock_data(stock):
        if shutdown_manager.is_shutdown(): return None, None
        frames = _fetch_data_for_stock(broker, stock, timeframes)
        if frames:
            return stock, frames
        logger.error(f"Error fetching data for {stock}", exc_info=False)
        return stock, None

    with ThreadPoolExecutor(max_workers=config.AppConfig.SAFE_API_WORKERS) as executor:
        futures = {executor.submit(fetch_stock_data, stock): stock for stock in symbols}
        for i, future in enumerate(as_completed(futures)):
            if shutdown_manager.is_shutdown():
                for f in futures: f.cancel()
                break

            elapsed = time_sleep.time() - start_time
            eta = (elapsed / (i + 1)) * (total - (i + 1)) if i > 0 else 0
            progress = f"Stage: Fetching {'/'.join(timeframes)} | {(i+1)/total:.1%} | {i+1}/{total} | ETA: {eta:.0f}s"
            print(f"\r{progress.ljust(70)}", end="")

            stock, frames = future.result()
            if frames:
                data_store.setdefault(stock, {}).update(frames)

    fetch_duration = time_sleep.time() - start_time
    print(f"\r{' ' * 70}\r", end="") # Clear line
    logger.info(f"Data fetch complete in {fetch_duration:.2f}s.")
    return data_store


def _generate_report(df: pd.DataFrame, regime_reason: str):
    """Formats, prints, and saves the EMFB report to multiple formats."""
    if df.empty:
        print("\nNo EMFB signals found.")
        return

    df = df.sort_values(by='EMFB_Score', ascending=False).reset_index(drop=True)
    df['Rank'] = df.index + 1

    print("\n" + "="*120)
    print("EMERGING MOMENTUM / FRESH BREAKOUT (EMFB) REPORT")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Regime Trigger: {regime_reason}")
    print("="*120)

    display_cols = [
        'Rank', 'Symbol', 'EMFB_Score', 'Confidence', 'RS_vs_Nifty', 'RS_vs_Sector',
        'Recovery', 'Closing', 'VWAP_Score', 'Last_Hour_Vol', 'Breakout_Score', 'Reason',
        'Trigger', 'Stop', 'Target', 'Data_Stale'
    ]
    
    report_df = df[[c for c in display_cols if c in df.columns]].copy()
    print(report_df.head(config.EMFB.REPORT_TOP_N).to_string(index=False))
    print("="*120)

    try:
        from sector_rotation import print_rs_only_rotation_report
        print_rs_only_rotation_report(df)
    except Exception as e:
        logger.warning(f"Sector rotation section skipped due to an error: {e}", exc_info=True)

    # Save the full results to multiple formats
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_filename = f"emfb_report_{timestamp_str}"
    try:
        df.to_csv(f"{base_filename}.csv", index=False)
        df.to_json(f"{base_filename}.json", orient='records', lines=True)
        df.to_parquet(f"{base_filename}.parquet", index=False)
        
        db = SignalDB(db_path='reports.db')
        db.log_dataframe(df, table_name='emfb_signals')
        
        logger.info(f"EMFB reports successfully saved to {base_filename}.[csv, json, parquet] and reports.db")
    except Exception as e:
        # Also print (not just log) - a silent reports.db write failure on
        # 2026-07-31/2026-08-04 went unnoticed because it only surfaced via
        # logger.error, which isn't guaranteed visible in every run context
        # (e.g. scheduled-task log redirection). CSV/JSON/parquet still save
        # fine even when this fails since they're written before this block.
        print(f"⚠️ Failed to save EMFB reports: {e}")
        logger.error(f"Failed to save EMFB reports: {e}", exc_info=True)


def _generate_portfolio_summary(df: pd.DataFrame):
    """Prints a summary of the top candidates."""
    if df.empty:
        return

    top_picks = df.head(config.EMFB.REPORT_TOP_N)
    print("\n" + "="*50)
    print("PORTFOLIO SUMMARY")
    print("="*50)
    print(f"Top Picks ({len(top_picks)}): {', '.join(top_picks['Symbol'].tolist())}")
    
    sector_alloc = top_picks['Sector'].value_counts(normalize=True) * 100
    print("\nSector Allocation:")
    for sector, pct in sector_alloc.items():
        print(f"  - {sector}: {pct:.1f}%")

    print("\nAverage Metrics (Top Picks):")
    avg_score = top_picks['EMFB_Score'].mean()
    avg_risk = ((top_picks['Trigger'] - top_picks['Stop']) / top_picks['Trigger']).mean() * 100
    avg_rr = ((top_picks['Target'] - top_picks['Trigger']) / (top_picks['Trigger'] - top_picks['Stop'])).mean()
    
    print(f"  - Avg EMFB Score: {avg_score:.1f}")
    print(f"  - Avg Risk/Trade: {avg_risk:.2f}%")
    print(f"  - Avg Risk:Reward: 1:{avg_rr:.2f}")
    print("="*50)


def run_emfb_scan(broker: Optional[DataBroker] = None, force_refresh: bool = False) -> pd.DataFrame:
    """Main orchestrator for the EMFB strategy.

    Staged flow so that a quiet market costs ~5 API calls instead of ~2000:
      1. Fetch daily data for the core indices, check index/VIX weakness.
      2. Fetch daily data for the universe (needed for A/D breadth and scanning).
      3. Only if the regime gate activates: fetch intraday timeframes + sector indices, scan.

    Returns the ranked EMFB DataFrame (empty if the scan didn't run or found nothing),
    so callers like `run_eod_scan` can compose it with other strategies' results.
    An existing `broker` may be passed in to reuse a session instead of logging in again.

    Checks the result-level cache first (see EMFB_CACHE_PATH above) - without
    this, `python main.py eod` right after a standalone `python main.py emfb`
    (or `eod` run twice) redoes the entire universe fetch, since most of EMFB's
    own timeframes expire from the raw candle cache in 50 seconds.
    """
    if not force_refresh:
        cached = _load_emfb_cache()
        if cached is not None:
            return cached

    if not _is_scan_window() and not config.EMFB_Regime.FORCE_RUN:
        return pd.DataFrame()

    start_time = time_sleep.time()
    broker = broker if broker is not None else DataBroker()
    scanner = EMFBScanner()

    provider = ConstituentProvider(index_name=config.EMFB.UNIVERSE_INDEX)
    universe = provider.get_universe() # Returns list of dicts with sector etc.
    symbols_only = [s['symbol'] for s in universe]

    # Stage 1: core indices only (cheap regime pre-check).
    data_store = _fetch_data_for_universe(broker, CORE_INDICES, ['daily'])
    if not data_store:
        logger.error("Failed to fetch index data. Aborting EMFB scan.")
        return pd.DataFrame()

    index_active, reasons = _check_index_regime(data_store)

    # Stage 2: universe dailies (needed for the A/D breadth check and for scanning).
    data_store = _fetch_data_for_universe(broker, symbols_only, ['daily'], data_store)
    breadth_active, breadth_reasons = _check_breadth_regime(data_store)
    reasons += breadth_reasons

    is_active = index_active or breadth_active
    if config.EMFB_Regime.FORCE_RUN and not is_active:
        logger.warning("FORCE_RUN is True. Running EMFB scan regardless of market regime.")
        is_active, reasons = True, ["Forced Run"]

    if not is_active:
        logger.info("EMFB Not Activated: Market conditions do not meet weakness criteria.")
        # Cache this too - it's a real, meaningful result ("checked, market isn't
        # weak"), not a failure. Unlike the fetch-failure returns above/below,
        # which are deliberately left uncached so a genuine broker/API problem
        # gets retried on the next call instead of silently serving a stale
        # "empty" result for up to an hour.
        empty_df = pd.DataFrame()
        _save_emfb_cache(empty_df)
        return empty_df
    logger.warning(f"EMFB Activated: Market is weak. Reason: {', '.join(reasons)}")

    nifty_df = data_store.get('Nifty 50', {}).get('daily')
    if nifty_df is None or nifty_df.empty:
        logger.error("Nifty 50 daily data unavailable. Aborting EMFB scan.")
        return pd.DataFrame()

    # Stage 3: the expensive fetches, only now that the scan is actually running.
    sector_indices_to_fetch = set()
    for stock_info in universe:
        sector_index = config.Universe.SECTOR_INDEX_MAP.get(stock_info.get('sector', 'OTHER'))
        if sector_index:
            sector_indices_to_fetch.add(sector_index)
    data_store = _fetch_data_for_universe(broker, sorted(sector_indices_to_fetch), ['daily'], data_store)
    data_store = _fetch_data_for_universe(broker, symbols_only, ['hourly', '15min', '5min'], data_store)

    market_regime_label = _get_market_regime_label(reasons)
    logger.info(f"Applying EMFB weight profile: {market_regime_label}")

    # --- Stage 1: Compute Raw Metrics in Parallel ---
    raw_metrics = []
    scan_start_time = time_sleep.time()

    def compute_metrics_for_stock(stock_info):
        if shutdown_manager.is_shutdown(): return None
        stock_symbol = stock_info['symbol']
        
        # Consolidate all data for the stock
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

        # EMFB isn't covered by orchestrator.py's CAS-window split (see run_manual_scan),
        # so this staleness flag - set by data_broker.py's fetch_ohlcv on the intraday
        # frames above - is the only signal a reader gets that an F&O name's EMFB
        # score may have been computed on a frozen pre-auction candle.
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
            logger.error(f"EMFB metric calculation error for {stock_symbol}: {e}", exc_info=False)
            return None

    with ThreadPoolExecutor(max_workers=config.AppConfig.SAFE_API_WORKERS) as executor:
        stocks_to_scan = [s for s in universe if s['symbol'] not in CORE_INDICES]
        total_to_scan = len(stocks_to_scan)
        futures = {executor.submit(compute_metrics_for_stock, stock_info): stock_info['symbol'] for stock_info in stocks_to_scan}
        
        for i, future in enumerate(as_completed(futures)):
            if shutdown_manager.is_shutdown():
                for f in futures: f.cancel()
                break

            elapsed = time_sleep.time() - scan_start_time
            eta = (elapsed / (i + 1)) * (total_to_scan - (i + 1)) if i > 0 else 0
            progress = f"Stage 1: Computing Metrics | {(i+1)/total_to_scan:.1%} | {i+1}/{total_to_scan} | ETA: {eta:.0f}s"
            print(f"\r{progress.ljust(80)}", end="")

            metric_result = future.result()
            if metric_result:
                raw_metrics.append(metric_result)

    scan_duration = time_sleep.time() - scan_start_time
    print(f"\r{' ' * 80}\r", end="") # Clear line
    logger.info(f"Stage 1 (Metrics) complete in {scan_duration:.2f}s. Processed {len(raw_metrics)} stocks.")

    if not raw_metrics:
        logger.info("No stocks passed initial metric calculation.")
        return pd.DataFrame()

    # --- Stage 2: Rank, Score, and Filter ---
    logger.info("Stage 2: Ranking universe and calculating final scores...")
    metrics_df = pd.DataFrame(raw_metrics).replace([np.inf, -np.inf], np.nan).dropna(subset=['close'])
    final_df = scanner.rank_and_score_emfb(metrics_df, market_regime_label)
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

    _generate_report(final_df, ", ".join(reasons))
    _generate_portfolio_summary(final_df)

    total_duration = time_sleep.time() - start_time
    logger.info(f"EMFB run finished in {total_duration:.2f} seconds.")
    _save_emfb_cache(final_df)
    return final_df
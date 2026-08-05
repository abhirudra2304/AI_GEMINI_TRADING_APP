import logging
from typing import Optional
from datetime import datetime
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from data_broker import DataBroker
from scanner_engine import HybridScanner
from utils import add_decision_scores
from lifecycle_manager import shutdown_manager
from database import SignalDB
from cache import DiscoveryCache, load_cached_discovery, save_topn_history, load_topn_history
from reporting import display_confirmation_results

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# NSE's Closing Auction Session (CAS) for F&O-eligible stocks runs 15:15-15:35;
# fetch_ohlcv() only detects the resulting stale-candle symptom (see
# data_broker.py's _flag_if_stale), it can't avoid it. This is the actual
# avoidance: defer scanning F&O names until the auction print is available,
# while non-F&O names (unaffected by CAS) still scan immediately.
_CAS_WINDOW_START = datetime.strptime("15:15", "%H:%M").time()
_CAS_WINDOW_END = datetime.strptime("15:35", "%H:%M").time()


def _in_cas_window(now: datetime) -> bool:
    market_time = now.astimezone(config.MARKET_TZ).time()
    return _CAS_WINDOW_START <= market_time < _CAS_WINDOW_END


def _scan_for_confirmation_signals(broker, scanner, watchlist, discovered_df, regime_mult, strategy, force_refresh, caller, point_in_time):
    """Scans a watchlist of stocks in parallel to find trading signals."""
    signals_triggered = []

    def scan_stock(stock):
        try:
            if shutdown_manager.is_shutdown(): return None
            # --- MODIFIED: Use the new live candle provider ---
            df_15min = broker.get_live_candles(stock, 'FIFTEEN_MINUTE')
            df_5min = pd.DataFrame()
            if strategy.upper() in ['BTST', 'GAP', 'INTRADAY']:
                df_5min = broker.get_live_candles(stock, 'FIVE_MINUTE')
            # --- END MODIFICATION ---
            is_stale = bool(df_15min.attrs.get('stale') or df_5min.attrs.get('stale'))

            if not df_15min.empty and stock in discovered_df['Symbol'].values:
                s_row = discovered_df[discovered_df['Symbol'] == stock].iloc[0]
                df_daily = s_row.get('_Daily_DF', pd.DataFrame())
                daily_metrics = s_row.get('_Daily_Metrics')
                if df_daily.empty:
                    return None
                # Only trust df_15min's last close as "live" for real-time scans.
                # For backtests (point_in_time set), get_live_candles() above has no
                # point_in_time concept and always returns real "today" data, so
                # treating it as live here would leak look-ahead bias into every
                # backtested signal's price/Stop/Target - leave live_close=None so
                # scan() falls back to Discovery's point_in_time-bounded Close instead.
                live_close = float(df_15min['Close'].iloc[-1]) if point_in_time is None else None
                signal = scanner.scan(
                    stock, df_daily, df_15min,
                    rs_percentile=s_row['RS_Pctl'],
                    sector_rs=s_row['Sector_RS'],
                    regime_mult=regime_mult,
                    strategy=strategy,
                    daily_metrics=daily_metrics,
                    df_5min=df_5min,
                    live_close=live_close,
                )
                # Surface data_broker.py's staleness flag (see fetch_ohlcv/_flag_if_stale)
                # onto the signal itself, so it reaches the report instead of only the logs.
                if signal is not None:
                    signal['Data_Stale'] = is_stale
                return signal
        except Exception as e:
            logger.error(f"Micro-scan error for {stock}: {e}")
        return None

    with ThreadPoolExecutor(max_workers=config.AppConfig.SAFE_API_WORKERS) as executor:
        futures = {executor.submit(scan_stock, stock): stock for stock in watchlist}
        for future in as_completed(futures):
            if shutdown_manager.is_shutdown():
                for f in futures: f.cancel()
                break
            signal = future.result()
            if signal:
                signals_triggered.append(signal)
    return signals_triggered

def _persist_confirmation_signals(signals_triggered):
    """Logs a list of signal dictionaries to the database."""
    db = SignalDB()
    try:
        if shutdown_manager.is_shutdown(): return
        for sig in signals_triggered:
            db.log_signal(sig)
    finally:
        db.close()

_TIER_ORDER = {
    'EVENT RISK 🛑': -1,
    'WEAK ⚠️': 0,
    'MODERATE ⚡': 1,
    'STRONG+ 🔥': 2,
    'VERY STRONG 🚀': 3,
}


def _log_topn_diff(df_signals: pd.DataFrame, discovered_df: pd.DataFrame, strategy: str, score_col: str) -> None:
    """Diffs this run's top-N confirmed signals against the previous run's for
    this exact strategy, logging dropped tickers (with an attributed reason,
    in priority order: Phase 1 drop, Phase 2 hard-filter/data failure,
    earnings veto, ranked below cutoff) and significant demotions (tier
    downgrade or score drop > 10 points) for tickers present in both.

    Always persists this run's own top-N as the new baseline for the *next*
    same-strategy run - see cache.py's save/load_topn_history.
    """
    top_n = config.Discovery.TOP_N_TRACKED_FOR_DIFF
    has_score_col = not df_signals.empty and score_col in df_signals.columns
    current_top = df_signals.sort_values(by=score_col, ascending=False).head(top_n) if has_score_col else pd.DataFrame()

    previous = load_topn_history(strategy)
    save_topn_history(current_top, strategy, score_col)

    if not previous or not previous.get("records"):
        logger.info(f"{strategy} scan diff vs previous {strategy} run: no prior baseline recorded yet.")
        return

    prev_records = {r["Symbol"]: r for r in previous["records"]}
    prev_symbols = set(prev_records.keys())
    current_top_symbols = set(current_top["Symbol"]) if not current_top.empty else set()
    all_signal_symbols = set(df_signals["Symbol"]) if not df_signals.empty else set()
    discovered_symbols = (
        set(discovered_df["Symbol"])
        if discovered_df is not None and not discovered_df.empty and "Symbol" in discovered_df.columns
        else set()
    )

    dropped = []
    for symbol in sorted(prev_symbols - current_top_symbols):
        if symbol not in discovered_symbols:
            reason = "Dropped from discovery watchlist (Phase 1)"
        elif symbol not in all_signal_symbols:
            reason = "Failed Phase 2 confirmation scan (hard filter or missing data - see debug logs for exact condition)"
        else:
            row = df_signals[df_signals["Symbol"] == symbol].iloc[0]
            if str(row.get("Execution_Recommendation", "")) == "AVOID (EARNINGS)":
                reason = "Earnings veto"
            else:
                rank = int((df_signals[score_col] > row[score_col]).sum()) + 1
                reason = f"Ranked below top-{top_n} cutoff (current rank: {rank}, score: {row[score_col]:.1f})"
        dropped.append(f"{symbol}: {reason}")

    demoted = []
    for symbol in sorted(prev_symbols & current_top_symbols):
        prev = prev_records[symbol]
        row = current_top[current_top["Symbol"] == symbol].iloc[0]
        old_score, new_score = float(prev["Score"]), float(row[score_col])
        old_tier, new_tier = str(prev["Tier"]), str(row.get("Strength", "UNKNOWN"))
        score_delta = new_score - old_score
        tier_downgraded = _TIER_ORDER.get(new_tier, 0) < _TIER_ORDER.get(old_tier, 0)
        if tier_downgraded or score_delta < -10:
            demoted.append(f"{symbol}: {old_tier} -> {new_tier}, score {old_score:.1f} -> {new_score:.1f}")

    header = f"{strategy} scan diff vs previous {strategy} run ({previous['saved_at']}):"
    if not dropped and not demoted:
        logger.info(f"{header}\n  (no changes)")
        return
    lines = [header]
    if dropped:
        lines.append("  DROPPED: " + "; ".join(dropped))
    if demoted:
        lines.append("  DEMOTED: " + "; ".join(demoted))
    logger.info("\n".join(lines))


def run_manual_scan(broker, scanner, watchlist, discovered_df, strategy: str = 'SWING', persist: bool = True, display: bool = True, force_refresh: bool = False, caller: str = "Unknown", point_in_time: Optional[datetime] = None) -> pd.DataFrame:
    logger.info(f"🔥 Phase 2: Running Micro-Volume Confirmation Scan for Best Stocks under {strategy}...")
    if '_Regime_Mult' in discovered_df.columns and not discovered_df.empty:
        regime_mult = discovered_df['_Regime_Mult'].iloc[0]
    else:
        nifty_df = broker.fetch_ohlcv('Nifty 50', 'ONE_DAY', 400, force_refresh=force_refresh, caller=caller, end_date=point_in_time)
        regime_mult = scanner.compute_market_regime(nifty_df)['multiplier']

    now = datetime.now(config.MARKET_TZ)
    # point_in_time is set only for backtests/historical replay, which have no real
    # wall-clock CAS window to wait out - never split/wait there. broker.fno_underlyings
    # empty means scrip_master.json didn't load or had no NFO rows; without it we can't
    # tell F&O from non-F&O, so fall back to the pre-existing unsplit behavior rather
    # than blocking the whole watchlist on a wait we can't justify.
    if point_in_time is None and _in_cas_window(now) and broker.fno_underlyings:
        fno_watchlist = [s for s in watchlist if broker.is_fno_eligible(s)]
        non_fno_watchlist = [s for s in watchlist if s not in fno_watchlist]
        logger.info(
            f"CAS window active ({now.strftime('%H:%M')}): scanning {len(non_fno_watchlist)} non-F&O "
            f"names now, deferring {len(fno_watchlist)} F&O names until after 15:35 (post-auction)."
        )
        signals_triggered = _scan_for_confirmation_signals(
            broker, scanner, non_fno_watchlist, discovered_df, regime_mult, strategy, force_refresh, caller, point_in_time
        )
        if fno_watchlist:
            wait_seconds = max(
                0.0,
                (datetime.combine(now.date(), _CAS_WINDOW_END) - now.replace(tzinfo=None)).total_seconds(),
            )
            logger.info(f"Waiting {wait_seconds:.0f}s for the CAS auction to close before scanning {len(fno_watchlist)} F&O names...")
            if shutdown_manager.shutdown_event.wait(timeout=wait_seconds):
                logger.warning("Shutdown requested during CAS wait; skipping deferred F&O scan.")
            else:
                signals_triggered += _scan_for_confirmation_signals(
                    broker, scanner, fno_watchlist, discovered_df, regime_mult, strategy, force_refresh, caller, point_in_time
                )
    else:
        signals_triggered = _scan_for_confirmation_signals(
            broker, scanner, watchlist, discovered_df, regime_mult, strategy, force_refresh, caller, point_in_time
        )
    score_col = 'BTST_Final_Score' if strategy.upper() in ['BTST', 'GAP', 'INTRADAY'] else 'Decision_Score'

    if not signals_triggered:
        logger.info("No actionable signals found during this scan.")
        # Still diff: a full collapse of the watchlist (everything dropped) is
        # exactly the case this logging exists to catch, not one to skip.
        # Backtests/historical generation (point_in_time set) replay many
        # synthetic "runs" per invocation with no real wall-clock gap between
        # them, which would corrupt topn_history's live-to-live baseline.
        if not point_in_time:
            _log_topn_diff(pd.DataFrame(), discovered_df, strategy, score_col)
        return pd.DataFrame()

    df_signals = add_decision_scores(pd.DataFrame(signals_triggered))
    if score_col not in df_signals.columns:
        score_col = 'Decision_Score'  # matches the original BTST_Final_Score-availability fallback
    df_signals = df_signals.sort_values(by=score_col, ascending=False)

    if not point_in_time:
        _log_topn_diff(df_signals, discovered_df, strategy, score_col)

    if display:
        display_confirmation_results(df_signals, strategy, discovered_df)

    if persist:
        _persist_confirmation_signals(signals_triggered)

    return df_signals

def run_fast_execution_scan(broker, scanner, strategy: str = 'BTST', top_n: int = 20, display: bool = True, force_refresh: bool = False, caller: str = "Unknown") -> pd.DataFrame:
    """Phase 2 fast path: reuse daily discovery state and fetch only 15-minute candles."""
    watchlist, discovered_df = load_cached_discovery(strategy=strategy, top_n=top_n)
    if not watchlist:
        logger.warning(
            "Discovery cache unavailable for %s. Run `python main.py cache %s` for details or `python main.py discover %s` to refresh Phase 1.",
            strategy,
            strategy,
            strategy,
        )
        return pd.DataFrame()
    logger.info(f"⚡ Fast execution scanner using top {len(watchlist)} cached symbols.")
    return run_manual_scan(broker, scanner, watchlist, discovered_df, strategy=strategy, persist=False, display=display, force_refresh=force_refresh, caller=caller)


def run_eod_scan(broker, scanner, top_n: int = 20, force_refresh: bool = False) -> dict:
    """End-of-day combiner: runs BTST, SWING (HybridScanner) and EMFB (EMFBScanner)
    sequentially and returns their ranked DataFrames for a single consolidated report.

    Each strategy's own discovery cache / regime gating is left untouched — this only
    sequences the three existing scan paths and collects their results, it does not
    change what each one filters or scores.

    Each stage is isolated: if one strategy's scan raises (broker/API hiccup, bad
    cache, etc.), it's logged and that stage comes back as an empty DataFrame
    instead of aborting the whole `eod` run and losing the other two strategies'
    results. `results['_errors']` carries which stages failed, if any.
    """
    # Local import avoids a circular import: emfb.py imports from scanner_engine,
    # which orchestrator.py already depends on at module load time.
    from emfb import run_emfb_scan

    results: dict = {}
    errors: dict = {}

    def _run_stage(name: str, fn, *args, **kwargs) -> pd.DataFrame:
        logger.info(f"🌇 EOD scan: running {name}...")
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.error(f"EOD scan stage '{name}' failed: {e}", exc_info=True)
            errors[name] = str(e)
            return pd.DataFrame()

    results['BTST'] = _run_stage(
        'BTST', run_fast_execution_scan, broker, scanner, strategy='BTST',
        top_n=top_n, display=False, force_refresh=force_refresh, caller="EOD",
    )
    results['SWING'] = _run_stage(
        'SWING', run_fast_execution_scan, broker, scanner, strategy='SWING',
        top_n=top_n, display=False, force_refresh=force_refresh, caller="EOD",
    )
    results['EMFB'] = _run_stage('EMFB', run_emfb_scan, broker=broker, force_refresh=force_refresh)

    results['_errors'] = errors
    return results

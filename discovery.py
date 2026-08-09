import logging
from typing import Optional
import os
import sys

# Add the project root to the Python path to resolve import issues
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from datetime import datetime
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from data_broker import DataBroker
from scanner_engine import HybridScanner
from lifecycle_manager import shutdown_manager
from provider import ConstituentProvider
from cache import DiscoveryCache
from market_regime import MarketRegime
from reporting import format_terminal_table

logger = logging.getLogger(__name__)


def _augment_with_institutional_score(discovered_df: pd.DataFrame) -> pd.DataFrame:
    """Adds an informational Institutional_Score column (0-100) to
    discovered_df, sourced from institutional_flow.py (an IAS plug-in
    module) using real NSE bhavcopy delivery data via nse_delivery_feed.py.

    Purely additive and read-only: never used in Rank_Score, sorting, or any
    filter, and this is the ONLY thing this function does to discovered_df -
    every other column and the row order are untouched. Validated against
    one month of real historical data (2026-08-09 session): Spearman rank
    correlation with 3-5 day forward returns was ~0, but stocks in the top
    quintile of the score beat the bottom quintile on 65-70% of days - a
    real but not yet individually-reliable signal. Stays informational-only
    (not wired into ranking/filtering) until it has 4-6+ weeks of real
    running history to judge - see SESSION_NOTES.md.

    Inherently a T-1 (lagged) indicator: NSE's bhavcopy for "today" isn't
    published until well after close, so this always reflects the most
    recent already-published trading day, never the live session.

    Failure policy: this must never break Discovery's existing output. Any
    problem - IAS modules unavailable, NSE bhavcopy unreachable, a holiday
    with no published file, an unexpected shape/error inside the engine -
    is logged and leaves Institutional_Score as NaN (all rows, or just the
    symbols affected), not raised. Both IAS imports below are local and
    wrapped in the same try/except for the same reason: an import-time
    error inside institutional_flow.py/nse_delivery_feed.py must not be
    able to crash `import discovery` for every other caller (main.py,
    orchestrator.py, ...), only disable this one optional column.
    """
    discovered_df = discovered_df.copy()
    discovered_df['Institutional_Score'] = float('nan')

    feed = None
    try:
        from nse_delivery_feed import NSEDeliveryFeed
        from institutional_flow import InstitutionalFlowEngine

        symbols = discovered_df['Symbol'].tolist()
        feed = NSEDeliveryFeed()
        history = feed.fetch_delivery_history(symbols, trading_days=1)
        if history.empty:
            logger.warning("Institutional_Score skipped: no recent NSE bhavcopy available.")
            return discovered_df
        target_date = history.index[0]

        engine_data = {}
        for _, row in discovered_df.iterrows():
            symbol = row['Symbol']
            daily_df = row.get('_Daily_DF')
            if daily_df is None or daily_df.empty:
                continue
            df = daily_df.copy()
            df['_d'] = pd.to_datetime(df['Timestamp']).dt.tz_localize(None).dt.normalize()
            df = df.drop_duplicates(subset='_d').set_index('_d').sort_index()
            df['DeliveryVolume'] = 0.0
            if target_date in df.index and symbol in history.columns:
                df.loc[target_date, 'DeliveryVolume'] = history.loc[target_date, symbol]
            engine_data[symbol] = df

        if not engine_data:
            logger.warning("Institutional_Score skipped: no usable daily history for any symbol.")
            return discovered_df

        engine = InstitutionalFlowEngine(engine_data)
        rvol = engine.calculate_rvol()
        delivery_pct = engine.calculate_delivery_percent()
        closing_range = engine.calculate_closing_range()
        institutional_score = engine.calculate_institutional_score(rvol, delivery_pct, closing_range)

        if target_date not in institutional_score.index:
            logger.warning(f"Institutional_Score skipped: {target_date.date()} not present in computed output.")
            return discovered_df

        scores = institutional_score.loc[target_date]
        discovered_df['Institutional_Score'] = discovered_df['Symbol'].map(scores)
        logger.info(
            f"Institutional_Score computed for {scores.notna().sum()}/{len(discovered_df)} "
            f"symbols (as of {target_date.date()})."
        )
    except Exception as e:
        logger.warning(f"Institutional_Score skipped due to an error: {e}", exc_info=True)
        discovered_df['Institutional_Score'] = float('nan')
    finally:
        if feed is not None:
            feed.close()

    return discovered_df


def get_universe_returns(broker, universe, lookback_days: int = 90, force_refresh: bool = False, caller: str = "Unknown", end_date: Optional[datetime] = None):
    returns = {}
    total = len(universe)
    daily_cache = {}

    def fetch_return(stock):
        if shutdown_manager.is_shutdown(): return stock, None
        df = daily_cache.get(stock)
        if df is None:
            df = broker.fetch_ohlcv(stock, 'ONE_DAY', 400, force_refresh=force_refresh, caller=caller, end_date=end_date)
            daily_cache[stock] = df
        if not df.empty and len(df) >= lookback_days:
            return stock, {
                'return': (df['Close'].iloc[-1] / df['Close'].iloc[-lookback_days]) - 1,
                'daily_df': df,
            }
        return stock, None

    with ThreadPoolExecutor(max_workers=config.AppConfig.SAFE_API_WORKERS) as executor:
        futures = {executor.submit(fetch_return, stock): stock for stock in universe}
        for i, future in enumerate(as_completed(futures)):
            if shutdown_manager.is_shutdown():
                # Cancel remaining futures
                for f in futures:
                    f.cancel()
                break
            print(f"[{i+1}/{total}] Fetching {lookback_days}-day returns...", end='\r')
            stock, stock_data = future.result()
            if stock_data is not None: returns[stock] = stock_data
    print(" " * 60, end='\r')
    return returns

def fetch_dynamic_universe(
    provider: ConstituentProvider,
    point_in_time: Optional[datetime] = None
) -> list:
    """
    Fetches the appropriate stock universe.
    - For historical runs (point_in_time is set), it uses the ConstituentProvider.
    - For live runs (point_in_time is None), it uses the default list from config.
    """
    universe = provider.get_universe(point_in_time)
    if not universe:
        logger.error("Failed to fetch a valid stock universe. Aborting discovery.")
        return []
    return universe

def execute_macro_discovery(broker: DataBroker, scanner: HybridScanner, strategy: str = 'SWING', force_refresh: bool = False, cache: DiscoveryCache = None, caller: str = "Unknown", point_in_time: Optional[datetime] = None, index_name: str = "nifty500") -> tuple:
    cache = cache or DiscoveryCache()
    if not force_refresh and cache.is_cache_valid(strategy):
        payload = cache.load_cache(strategy)
        discovered_df = payload.get("discovered_df")
        watchlist = discovered_df.head(config.Discovery.TOP_N_WATCHLIST)["Symbol"].tolist() if discovered_df is not None else []
        logger.info(f"⚡ Phase 1 cache hit: loaded {len(discovered_df)} ranked symbols from {cache.path}.")
        return watchlist, discovered_df, None

    logger.info(f"⚡ Phase 1: Initiating Broad Market Discovery Scan for {strategy}...")
    discovered_candidates = []
    if not point_in_time: broker._ensure_session()
    nifty_df = broker.fetch_ohlcv('Nifty 50', 'ONE_DAY', 400, force_refresh=force_refresh, caller=caller, end_date=point_in_time)
    regime = scanner.compute_market_regime(nifty_df)

    lookback_window = config.Discovery.LOOKBACK_BTST if strategy in ['BTST', 'GAP', 'INTRADAY'] else config.Discovery.LOOKBACK_SWING
    
    constituent_provider = ConstituentProvider(index_name=index_name)
    universe_records = fetch_dynamic_universe(constituent_provider, point_in_time)
    # Provider returns [{'symbol': ..., 'sector': ...}]; the broker and scan loop work on symbol strings.
    current_universe = [record['symbol'] for record in universe_records]
    sector_by_symbol = {record['symbol']: record.get('sector', 'OTHER') for record in universe_records}
    available_universe = broker.filter_available_symbols(current_universe)
    if not available_universe: return [], pd.DataFrame(), None
        
    raw_returns = get_universe_returns(broker, available_universe, lookback_days=lookback_window, force_refresh=force_refresh, caller=caller, end_date=point_in_time)
    if not raw_returns: return [], pd.DataFrame(), None

    if not point_in_time:
        # Sanity check for a mass stale-cache event (see 2026-07-29 incident:
        # a rate-limited fetch left 216/220 tickers silently scoring off
        # yesterday's close). Skipped for historical/backtest runs
        # (point_in_time set), where every ticker legitimately shares the
        # same as-of date and this check would be meaningless.
        last_bar_dates = pd.Series({stock: data['daily_df']['Timestamp'].max() for stock, data in raw_returns.items()})
        most_common_date = last_bar_dates.mode().iloc[0]
        fresh_count = int((last_bar_dates == most_common_date).sum())
        fresh_fraction = fresh_count / len(last_bar_dates)
        if fresh_fraction < config.Discovery.MIN_FRESH_DATA_FRACTION:
            print(
                f"\n⚠️ STALE DATA WARNING: only {fresh_count}/{len(last_bar_dates)} "
                f"({fresh_fraction:.0%}) of the fetched universe shares the most "
                f"recent close ({most_common_date.date()}). The rest are scoring off "
                "an older cached session (likely a rate-limited/failed fetch) - "
                "today's discovery ranking may not reflect current prices."
            )
            logger.warning(
                "execute_macro_discovery: stale data for %d/%d tickers (only %.0f%% fresh, most recent=%s)",
                len(last_bar_dates) - fresh_count, len(last_bar_dates), fresh_fraction * 100, most_common_date.date(),
            )

    ret_series = pd.Series({stock: data['return'] for stock, data in raw_returns.items()})
    
    sector_groups = {}
    for stock, stock_data in raw_returns.items():
        ret = stock_data.get('return', 0)
        sector = sector_by_symbol.get(stock) or scanner.SECTOR_MAP.get(stock, 'OTHER')
        sector_groups.setdefault(sector, []).append(ret)
    sector_averages = {k: sum(v)/len(v) for k, v in sector_groups.items()}

    def process_stock(stock):
        try:
            if shutdown_manager.is_shutdown(): return None
            if stock not in raw_returns: return None

            stock_data = raw_returns[stock]
            df_daily = stock_data['daily_df']
            if df_daily.empty: return None

            metrics = scanner.compute_daily_metrics(df_daily)
            if metrics is None: return None

            # Institutional RS Logic
            stock_ret = stock_data['return']
            rs_pctl = (ret_series < stock_ret).mean() * 100

            # Safely handle SECTOR_MAP
            sector_map = getattr(scanner, 'SECTOR_MAP', {})
            sector = sector_by_symbol.get(stock) or sector_map.get(stock, 'OTHER')

            sec_avg = sector_averages.get(sector, 0)
            sector_rs = max(0, min(100, 50 + (stock_ret - sec_avg) * 100))

            cp = metrics['Close']
            rsi = metrics.get('RSI', 50)

            # HARD FILTER: Drop Illiquid Stocks permanently
            if metrics.get('Avg_Traded_Value_20d', 0) < (config.Discovery.LIQUIDITY_THRESHOLD_CRORES * 1_00_00_000):
                return None

            # Calculate Daily Volume Ratio
            recent_vol = df_daily['Volume'].iloc[-3:].mean()
            historic_vol = df_daily['Volume'].iloc[-23:-3].mean()
            daily_vol_ratio = recent_vol / historic_vol if historic_vol > 0 else 0

            # Circuit Breaker: High Volume Reversal for short-term strategies
            is_volume_shock = (strategy in ['BTST', 'GAP', 'INTRADAY']) and (daily_vol_ratio > config.Discovery.VOL_SHOCK_RATIO_DAILY) and (rsi > config.Discovery.RSI_THRESHOLD - 10)

            # Enforce Hard Trend Filters ONLY if a Volume Shock is NOT present
            if not is_volume_shock:
                safe_ema20 = metrics.get('EMA20', metrics.get('EMA50', cp))
                trend_baseline = safe_ema20 if regime['label'] in ['BEARISH_RECOVERY', 'EXTREME_BEAR'] else metrics.get('EMA50', cp)
                if cp < trend_baseline or rs_pctl < config.Discovery.RS_PCT_THRESHOLD or rsi < config.Discovery.RSI_THRESHOLD:
                    return None

            pivot = metrics.get('Pivot_50', cp)
            if pd.isna(pivot) or pivot == 0:
                pct_from_pivot = 0.0
            else:
                pct_from_pivot = ((cp - pivot) / pivot) * 100

            distance_penalty_multiplier = 0.2 if is_volume_shock else 0.5
            distance_divisor = max(1.0, abs(pct_from_pivot) * distance_penalty_multiplier)

            # Institutional Ranking Formula
            safe_adx = metrics.get('adx', metrics.get('ADX', 0))
            rank_score = (
                (min(safe_adx, 50) * 0.4) +
                (min(rs_pctl, 100) * 0.8) +
                (min(sector_rs, 100) * 0.4) +
                (min(rsi, 80) * 0.2)
            ) / distance_divisor
            rank_score = min(rank_score, 100)

            safe_ema20_delta = metrics.get('EMA20', cp)

            return {
                'Symbol': stock,
                'Sector': sector,
                'RS_Pctl': rs_pctl,
                'Sector_RS': sector_rs,
                'ADX': safe_adx,
                'Distance': pct_from_pivot,
                'Rank_Score': rank_score,
                'EMA_Delta': ((cp - safe_ema20_delta) / safe_ema20_delta) * 100 if safe_ema20_delta > 0 else 0,
                'EMA20': metrics.get('EMA20', cp),
                'EMA50': metrics.get('EMA50', cp),
                'EMA200': metrics.get('EMA200', cp),
                'RSI': rsi,
                'Trend': 'BULL' if cp > (metrics.get('EMA50', cp) or cp) else 'BEAR',
                'Liquidity': 'HIGH' if metrics.get('Avg_Traded_Value_20d', 0) > 100_000_000 else 'LOW',
                'Market_Regime': regime['label'],
                '_Daily_DF': df_daily,
                '_Daily_Metrics': metrics,
                '_Regime_Mult': regime['multiplier'],
            }
        except Exception as e:
            logger.error(f"Discovery Crash on {stock}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=config.AppConfig.SAFE_API_WORKERS) as executor:
        futures = {executor.submit(process_stock, stock): stock for stock in available_universe}
        for future in as_completed(futures):
            if shutdown_manager.is_shutdown():
                for f in futures: f.cancel()
                break
            res = future.result()
            if res: discovered_candidates.append(res)
            
    if shutdown_manager.is_shutdown(): return [], pd.DataFrame(), None
    discovered_df = pd.DataFrame(discovered_candidates)
    if discovered_df.empty: return [], pd.DataFrame(), None

    discovered_df = discovered_df.sort_values(by='Rank_Score', ascending=False)

    if not point_in_time:
        # Informational-only IAS field, not part of Rank_Score/sorting/filtering -
        # see _augment_with_institutional_score's docstring. Skipped for
        # backtests/historical runs (point_in_time set): it's a live NSE fetch
        # with no point-in-time concept, and hasn't been validated for replay use.
        discovered_df = _augment_with_institutional_score(discovered_df)

    top_execution_watchlist = discovered_df.head(config.Discovery.TOP_N_WATCHLIST)['Symbol'].tolist()
    
    regime_analyzer = MarketRegime(broker, scanner, current_universe, discovered_df)
    regime_analyzer.calculate_regime()
    if not point_in_time:
        # This report (breadth, VIX, distribution days, sector leadership, a
        # Full/Half Position/Wait/No-BTST recommendation) was already computed
        # on every discovery run but never surfaced anywhere - every caller
        # discarded regime_analyzer after receiving it. Surfacing it here
        # covers every entry point (discover/btst/swing/eod/live) in one
        # place instead of touching each call site. Skipped for point-in-time
        # (backtest/historical-generation) runs, which call this in a loop
        # across many dates and would otherwise flood the console.
        regime_analyzer.print_report()

    cache.save_cache(discovered_df, top_execution_watchlist, strategy, regime)
    return top_execution_watchlist, discovered_df, regime_analyzer

import os
import sys
from datetime import datetime
from dotenv import load_dotenv

import pandas as pd

import config
from data_broker import DataBroker
from scanner_engine import HybridScanner
from profiler import profiler
from orchestrator import (
    DiscoveryCache,
    execute_macro_discovery,
    format_terminal_table,
    load_last_signals,
    run_fast_execution_scan,
    run_report_pipeline,    
    save_last_signals,
    start_background_report,
)
from utils import add_decision_scores

def build_runtime():
    broker = DataBroker()
    scanner = HybridScanner(base_multiplier=2.0, alpha=0.5, max_risk_per_trade=5000, max_capital_per_trade=100000)
    return broker, scanner


def print_discovery_views(full_df):
    if full_df.empty:
        return
    print("\n" + "=" * 40)
    print("📊 SECTOR LEADERSHIP DISTRIBUTION")
    print(full_df["Sector"].value_counts())

    print("\n" + "=" * 40)
    print("🏆 TOP 10 RS RANKINGS")
    df_rs_slice = full_df.sort_values(by="RS_Pctl", ascending=False).head(10)
    rs_str = df_rs_slice[["Symbol", "RS_Pctl", "Sector"]].to_string(index=False)
    print(format_terminal_table(rs_str, df_rs_slice))

    print("\n" + "=" * 40)
    print("🚀 TOP 10 VELOCITY (ADX) RANKINGS")
    df_adx_slice = full_df.sort_values(by="ADX", ascending=False).head(10)
    adx_str = df_adx_slice[["Symbol", "ADX", "Sector"]].to_string(index=False)
    print(format_terminal_table(adx_str, df_adx_slice))


def run_discovery(strategy: str = "SWING", force_refresh: bool = True):
    profiler.reset()
    print(f"🚀 Phase 1 discovery refresh for {strategy.upper()} setups...")
    broker, scanner = build_runtime()
    _, full_df = execute_macro_discovery(broker, scanner, strategy=strategy, force_refresh=force_refresh, cache=DiscoveryCache(), caller="Discovery")
    print_discovery_views(full_df)
    profiler.save_report()
    profiler.print_report()
    return full_df


def run_execution(strategy: str = "BTST", force_refresh: bool = False):
    profiler.reset()
    print(f"🚀 Fast {strategy.upper()} scanner starting...")
    print("🧭 Manual evaluation only — no automated orders will be placed.")
    broker, scanner = build_runtime()

    if force_refresh:
        print(f"♻️ Force refresh requested. Rebuilding {strategy.upper()} discovery cache first...")
        execute_macro_discovery(broker, scanner, strategy=strategy, force_refresh=True, cache=DiscoveryCache(), caller="Discovery")

    # Phase 2 deliberately reads Phase 1 output and fetches only intraday candles for top ranked stocks.
    df_signals = run_fast_execution_scan(broker, scanner, strategy=strategy, top_n=config.Discovery.TOP_N_FAST_SCAN, display=True, force_refresh=force_refresh, caller="Scanner")
    if df_signals.empty:
        metadata = DiscoveryCache().get_cache_metadata(strategy=strategy)
        if metadata["valid"]:
            print(f"\n⚠️ No actionable {strategy.upper()} signals found from the current discovery cache.")
        else:
            print(f"\n⚠️ {strategy.upper()} discovery cache is invalid: {metadata['invalid_reason']}.")
            print(
                f"Generated={metadata['generated_time'].isoformat(timespec='seconds') if metadata['generated_time'] else 'N/A'} | "
                f"Current={metadata['current_time'].isoformat(timespec='seconds') if metadata['current_time'] else 'N/A'} | "
                f"Age={metadata['age_text']} | TTL={metadata['ttl_text']}"
            )
            print(f"Run `python main.py cache {strategy.upper()}` for metadata or `python main.py discover {strategy.upper()}` to refresh Phase 1.")
        profiler.save_report()
        profiler.print_report()
        return df_signals

    save_last_signals(df_signals)

    start_background_report(df_signals, strategy=strategy)
    print("📄 Report, Gemini news, and database archival are running in the background.")
    profiler.save_report()
    profiler.print_report()
    return df_signals


def run_report_only():
    payload = load_last_signals()
    if not payload:
        print("⚠️ No saved signal set found. Run `python main.py btst` or `python main.py swing` first.")
        return
    created_at = payload.get("created_at", "unknown time")
    df_signals = payload.get("signals")
    print(f"📄 Building report from last saved signals ({created_at})...")
    run_report_pipeline(df_signals, strategy="report")


def run_system_check():
    """
    Performs a series of checks to ensure the system is configured correctly.
    """
    print('\n' + '=' * 50)
    print('⚙️  RUNNING SYSTEM HEALTH CHECK')
    print('=' * 50)

    # 1. Check for .env file and essential keys
    print('\n[1/5] Checking Environment Configuration (.env)...')
    dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(dotenv_path):
        print('  ❌ CRITICAL: `.env` file not found. Please create it by copying `example.env` and filling in your credentials.')
        return

    load_dotenv(dotenv_path=dotenv_path)
    required_keys = ['ANGEL_API_KEY', 'ANGEL_CLIENT_CODE', 'ANGEL_PASSWORD', 'ANGEL_TOTP_KEY', 'GEMINI_API_KEY']
    all_keys_found = True
    for key in required_keys:
        if os.getenv(key):
            print(f'  ✅ {key}: Found')
        else:
            print(f'  ❌ {key}: NOT FOUND in .env file.')
            all_keys_found = False
    if not all_keys_found:
        print('  👉 Please ensure all required API keys and credentials are in your .env file.')
    else:
        print('  👍 Environment configuration looks good.')

    # 2. Check Angel One API Connection
    print('\n[2/5] Checking Angel One API Connection...')
    try:
        broker = DataBroker()
        if broker.jwt_token:
            print('  ✅ Angel One session generated successfully.')
            nifty_df = broker.fetch_ohlcv('Nifty 50', 'ONE_DAY', 5, caller='HealthCheck')
            if not nifty_df.empty:
                print(f"  ✅ Successfully fetched Nifty 50 data (Last Close: {nifty_df['Close'].iloc[-1]}).")
            else:
                print('  ⚠️ Session generated, but failed to fetch Nifty 50 data. Check API permissions or network.')
        else:
            print('  ❌ Failed to generate Angel One session. Check credentials in .env file.')
    except Exception as e:
        print(f'  ❌ An error occurred while connecting to Angel One: {e}')

    # 3. Check Gemini API Connection
    print('\n[3/5] Checking Google Gemini API Connection...')
    from orchestrator import fetch_gemini_news
    try:
        news = fetch_gemini_news(['RELIANCE'])
        if '⚠️' in news:
            print(f'  ❌ Gemini API returned a warning/error: {news}')
        else:
            print('  ✅ Gemini API connection successful and returned a response.')
    except Exception as e:
        print(f'  ❌ An error occurred while connecting to Gemini API: {e}')

    # 4. Check Cache Status
    print('\n[4/5] Checking Discovery Cache Status...')
    run_cache_status()

    # 5. Suggest running tests
    print('\n[5/5] Checking Test Suite...')
    print('  👉 To validate indicator calculations, run: `pytest`')

    print('\n' + '=' * 50)
    print('HEALTH CHECK COMPLETE')
    print('=' * 50)


def get_cached_discovery_row(symbol: str, preferred_strategy: str = "SWING"):
    """Finds the full discovery data row for a symbol from any valid cache."""
    normalized_symbol = symbol.upper()
    # Start with the preferred strategy, then check others as a fallback.
    for strategy in dict.fromkeys([preferred_strategy, "BTST", "SWING", "GAP"]):
        cache = DiscoveryCache()
        if not cache.is_cache_valid(strategy):
            continue
        payload = cache.load_cache(strategy)
        discovered_df = payload.get("discovered_df") if payload else None
        if discovered_df is None or discovered_df.empty or "Symbol" not in discovered_df.columns:
            continue
        # Case-insensitive match on the symbol
        matched = discovered_df[discovered_df["Symbol"].astype(str).str.upper() == normalized_symbol]
        if not matched.empty:
            return matched.iloc[0], strategy  # Return the full row and which strategy cache it came from
    return None, None


def suggested_holding_period(row: pd.Series) -> str:
    strength = str(row.get("Strength", "")).upper()
    if "VERY STRONG" in strength:
        return "5-10 trading sessions"
    if "STRONG" in strength:
        return "3-7 trading sessions"
    if "MODERATE" in strength:
        return "2-5 trading sessions"
    return "Watch only"


def holding_recommendation(signal, metrics, rs_percentile: float, decision_score: float) -> str:
    close = float(metrics.get("Close", 0))
    ema50 = float(metrics.get("EMA50", close) or close)
    rsi = float(metrics.get("RSI", 50))
    liquidity = float(metrics.get("Avg_Traded_Value_20d", 0))

    if liquidity <= 100_000_000 or close < ema50 or rsi < 50 or rs_percentile < 40:
        return "EXIT"
    if signal and decision_score >= 65:
        return "HOLD"
    return "WATCH"


def explain_decision_score(row: pd.Series):
    score = float(row.get("Score", 0))
    adx = min(float(row.get("ADX", 0)), 50.0)
    rs = min(max(float(row.get("RS_Pctl", 0)), 0.0), 100.0)
    ema_distance = min(abs(float(row.get("EMA50_Distance", 0))), 30.0)

    score_part = score * 0.60
    adx_part = (adx / 50.0) * 100 * 0.15
    rs_part = rs * 0.15
    ema_part = (100 - ema_distance) * 0.10
    total = score_part + adx_part + rs_part + ema_part

    print("\nDecision Score explanation")
    print(f"  Scanner Score:   {score:.1f} x 60% = {score_part:.1f}")
    print(f"  ADX:             {adx:.1f}/50 x 15 = {adx_part:.1f}")
    print(f"  RS Percentile:   {rs:.1f} x 15% = {rs_part:.1f}")
    print(f"  EMA50 Distance:  (100 - {ema_distance:.1f}) x 10% = {ema_part:.1f}")
    print(f"  Final:           {total:.1f}")


def intraday_volume_ratio(df_15min: pd.DataFrame) -> float:
    if df_15min.empty or len(df_15min) < 4:
        return 0.0
    recent_vol_avg = df_15min["Volume"].iloc[-3:].mean()
    historical_avg_vol = df_15min["Volume"].iloc[:-3].tail(40).median()
    return min(recent_vol_avg / historical_avg_vol, 6.0) if historical_avg_vol > 0 else 0.0


def _print_analysis_report(report_data: dict):
    """Prints a formatted analysis report from a dictionary."""
    print("\n" + "=" * 80)
    print(f"{report_data.get('symbol', 'UNKNOWN')} ANALYZE RESULT")
    print(f"  Recommendation:            {report_data.get('recommendation', 'N/A')}")
    print(f"  Decision Score:            {report_data.get('decision_score', 'N/A')}")
    print(f"  Strength:                  {report_data.get('strength', 'N/A')}")
    print(f"  Trend:                     {report_data.get('trend', 'N/A')}")
    print(f"  ADX:                       {report_data.get('adx', 'N/A')}")
    print(f"  RSI:                       {report_data.get('rsi', 'N/A')}")
    print(f"  RS Percentile:             {report_data.get('rs_percentile', 'N/A')} ({report_data.get('rs_source', 'N/A')})")
    print(f"  Sector RS:                 {report_data.get('sector_rs', 'N/A')}")
    print(f"  Liquidity:                 {report_data.get('liquidity', 'N/A')}")
    print(f"  Breakout Status:           {report_data.get('breakout_status', 'N/A')}")
    print(f"  Volume Ratio:              {report_data.get('volume_ratio', 'N/A')}")
    print(f"  Stop:                      {report_data.get('stop', 'N/A')}")
    print(f"  Target:                    {report_data.get('target', 'N/A')}")
    print(f"  Risk:                      {report_data.get('risk', 'N/A')}")
    print(f"  Suggested Holding Period:  {report_data.get('holding_period', 'N/A')}")
    print("=" * 80)


def _get_analysis_data(broker: DataBroker, scanner: HybridScanner, symbol: str, force_refresh: bool):
    """
    Fetches all required data for a single-stock analysis, prioritizing cache.

    Returns a tuple of (daily_df, daily_metrics, df_15min, nifty_df, rs_percentile, sector_rs, rs_source)
    or (None, ...) if essential data is missing.
    """
    caller_context = "Analyze"

    # 1. Attempt to load daily data and pre-computed metrics from discovery cache
    cached_row, rs_source = None, "neutral default"
    if not force_refresh:
        cached_row, rs_source = get_cached_discovery_row(symbol)

    df_daily, daily_metrics = pd.DataFrame(), None
    rs_percentile, sector_rs = 50.0, 50.0

    if cached_row is not None:
        print(f"✔️ Using cached daily data for {symbol} from '{rs_source}' discovery cache.")
        df_daily = cached_row.get('_Daily_DF', pd.DataFrame())
        daily_metrics = cached_row.get('_Daily_Metrics')
        rs_percentile = float(cached_row.get("RS_Pctl", 50.0))
        sector_rs = float(cached_row.get("Sector_RS", 50.0))

    # 2. If cached data is missing or incomplete, fetch from the API.
    if df_daily.empty:
        df_daily = broker.fetch_ohlcv(symbol, "ONE_DAY", 400, force_refresh=force_refresh, caller=caller_context)
    if daily_metrics is None:
        daily_metrics = scanner.compute_daily_metrics(df_daily)

    # 3. Fetch intraday and market data (always fresh).
    df_15min = broker.fetch_ohlcv(symbol, "FIFTEEN_MINUTE", 5, force_refresh=force_refresh, caller=caller_context)
    nifty_df = broker.fetch_ohlcv("Nifty 50", "ONE_DAY", 400, force_refresh=force_refresh, caller=caller_context)

    # 4. Validate data
    if df_daily.empty or daily_metrics is None:
        print(f"⚠️ Could not retrieve or compute daily metrics for {symbol}.")
        return None, None, None, None, None, None, None
    if df_15min.empty:
        print(f"⚠️ No 15-minute candles found for {symbol}.")
        return None, None, None, None, None, None, None

    return df_daily, daily_metrics, df_15min, nifty_df, rs_percentile, sector_rs, rs_source


def _build_analysis_report(symbol: str, signal: dict, daily_metrics: dict, df_15min: pd.DataFrame, rs_percentile: float, sector_rs: float, rs_source: str, holding: bool) -> tuple[dict, pd.DataFrame]:
    """Builds the data dictionary for the analysis report and the ranked DataFrame."""
    close = float(daily_metrics.get("Close", 0))
    ema50 = float(daily_metrics.get("EMA50", close) or close)
    pivot_250 = daily_metrics.get("Pivot_250", close)

    report_data = {
        "symbol": symbol,
        "rs_source": rs_source,
        "trend": 'BULL' if close > ema50 else 'BEAR',
        "adx": f"{daily_metrics.get('adx', 0):.1f}",
        "rsi": f"{daily_metrics.get('RSI', 50):.1f}",
        "rs_percentile": f"{rs_percentile:.1f}",
        "sector_rs": f"{sector_rs:.1f}",
        "liquidity": "HIGH" if daily_metrics.get("Avg_Traded_Value_20d", 0) > 100_000_000 else "LOW",
        "breakout_status": "YES" if pd.notna(pivot_250) and close > pivot_250 else "NO",
        "volume_ratio": f"{intraday_volume_ratio(df_15min):.2f}",
    }

    ranked_df = pd.DataFrame()
    if not signal:
        print(f"⚠️ {symbol} did not pass the existing scanner hard filters.")
        report_data.update({
            "recommendation": holding_recommendation(None, daily_metrics, rs_percentile, 0) if holding else "NO BUY",
            "strength": "FILTERED",
        })
    else:
        ranked_df = add_decision_scores(pd.DataFrame([signal]))
        row = ranked_df.iloc[0]
        report_data.update({
            "recommendation": holding_recommendation(signal, daily_metrics, rs_percentile, row["Decision_Score"]) if holding else "BUY",
            "decision_score": row.get('Decision_Score', 'N/A'),
            "strength": row.get('Strength', 'N/A'),
            "trend": row.get('Trend', 'N/A'),
            "adx": row.get('ADX', 'N/A'),
            "rsi": row.get('RSI', 'N/A'),
            "rs_percentile": row.get('RS_Pctl', 'N/A'),
            "sector_rs": row.get('Sector_RS', 'N/A'),
            "liquidity": row.get('Liquidity', 'N/A'),
            "breakout_status": row.get('Breakout250', 'N/A'),
            "volume_ratio": row.get('Vol_Ratio', 'N/A'),
            "stop": row.get('Stop', 'N/A'),
            "target": row.get('Target', 'N/A'),
            "risk": f"{row.get('Risk_Level', 'N/A')} | RR {row.get('Risk_Reward', 'N/A')}",
            "holding_period": suggested_holding_period(row),
        })

    return report_data, ranked_df


def run_analyze(symbol: str, explain: bool = False, holding: bool = False, force_refresh: bool = False):
    profiler.reset()
    symbol = symbol.strip().upper()
    if not symbol:
        print("⚠️ Please provide a symbol. Example: python main.py analyze HAL")
        return pd.DataFrame()

    print(f"🔎 Analyzing {symbol} only...")
    broker, scanner = build_runtime()

    # 1. Fetch all data
    df_daily, daily_metrics, df_15min, nifty_df, rs_percentile, sector_rs, rs_source = _get_analysis_data(
        broker, scanner, symbol, force_refresh
    )

    if df_daily is None:
        profiler.save_report()
        profiler.print_report()
        return pd.DataFrame()

    # 2. Run the final analysis with the combined data.
    regime = scanner.compute_market_regime(nifty_df)
    signal = scanner.scan(
        symbol,
        df_daily,
        df_15min,
        rs_percentile=rs_percentile,
        sector_rs=sector_rs,
        regime_mult=regime["multiplier"],
        strategy="SWING",
        daily_metrics=daily_metrics,
    )

    # 3. Build and print the report
    report_data, ranked_df = _build_analysis_report(
        symbol, signal, daily_metrics, df_15min, rs_percentile, sector_rs, rs_source, holding
    )
    _print_analysis_report(report_data)

    # 4. Handle optional flags and cleanup
    if explain and not ranked_df.empty:
        explain_decision_score(ranked_df.iloc[0])

    profiler.save_report()
    profiler.print_report()
    return ranked_df


def print_single_cache_status(strategy: str = None):
    cache = DiscoveryCache()
    metadata = cache.get_cache_metadata(strategy=strategy)
    generated_time = metadata["generated_time"].isoformat(timespec="seconds") if metadata["generated_time"] else "N/A"
    current_time = metadata["current_time"].isoformat(timespec="seconds") if metadata["current_time"] else "N/A"
    requested = metadata["expected_strategy"] or "any"
    validity = "VALID" if metadata["valid"] else f"INVALID - {metadata['invalid_reason']}"

    print("Discovery cache metadata")
    print(f"  Path:            {metadata['path']}")
    print(f"  Exists:          {metadata['exists']}")
    print(f"  Generated time:  {generated_time}")
    print(f"  Current time:    {current_time}")
    print(f"  Age:             {metadata['age_text']}")
    print(f"  TTL:             {metadata['ttl_text']}")
    print(f"  Remaining time:  {metadata['remaining_text']}")
    print(f"  Strategy:        {metadata['strategy'] or 'N/A'}")
    print(f"  Requested:       {requested}")
    print(f"  Stocks:          {metadata['stock_count']}")
    print(f"  Market hours:    {metadata['market_hours']}")
    if metadata["legacy_fallback"]:
        print("  Source:          legacy discovery_cache.pkl fallback")
    print(f"  Validity:        {validity}")


def run_cache_status(strategy: str = None):
    if strategy:
        print_single_cache_status(strategy=strategy)
        return

    for index, strategy_name in enumerate(["BTST", "SWING", "GAP"]):
        if index:
            print()
        print_single_cache_status(strategy=strategy_name)

def run_profiler_report():
    """Loads and prints the last saved profiler report."""
    from profiler import APIProfiler
    p = APIProfiler()
    if p.load_report():
        p.print_report(from_cache=True)

def print_help():
    print("  python main.py status                                        # Run a system health check")
    print("Usage:")
    print("  python main.py analyze SYMBOL [--explain] [--holding]         # analyze one stock only")
    print("  python main.py discover [BTST|SWING|GAP] [--force-refresh]   # refresh discovery cache only")
    print("  python main.py cache [BTST|SWING|GAP]                        # show discovery cache metadata")
    print("  python main.py profiler                                      # show the last run's API profiler report")
    print("  python main.py btst       # fast BTST scan from cached discovery")
    print("  python main.py swing      # fast SWING scan from cached discovery")
    print("  python main.py gap        # fast GAP scan from cached discovery")
    print("  python main.py btst --force-refresh   # rebuild BTST cache, then scan")
    print("  python main.py report     # create report from last displayed signals")
    print("  python main.py invalidate [BTST|SWING|GAP] # delete discovery cache")


def parse_cli(argv):
    known_flags = {"--force-refresh", "--explain", "--holding"}
    flags = {
        "force_refresh": "--force-refresh" in argv,
        "explain": "--explain" in argv,
        "holding": "--holding" in argv,
    }
    positionals = [arg for arg in argv if not arg.startswith("--")]
    unknown_flags = [arg for arg in argv if arg.startswith("--") and arg not in known_flags]
    return positionals, flags, unknown_flags


if __name__ == "__main__":
    from lifecycle_manager import shutdown_manager
    from orchestrator import _background_workers

    positionals, flags, unknown_flags = parse_cli(sys.argv[1:])
    for flag in unknown_flags:
        print(f"⚠️ Unknown option '{flag}' ignored.")

    mode = positionals[0].strip().lower() if positionals else "btst"
    should_wait = False

    try:
        if mode == "status":
            run_system_check()
        elif mode == "discover":
            strategy = positionals[1].strip().upper() if len(positionals) > 1 else "BTST"
            run_discovery(strategy=strategy, force_refresh=flags["force_refresh"])
        elif mode == "analyze":
            symbol = positionals[1] if len(positionals) > 1 else ""
            run_analyze(symbol, explain=flags["explain"], holding=flags["holding"], force_refresh=flags["force_refresh"])
        elif mode == "cache":
            strategy = positionals[1].strip().upper() if len(positionals) > 1 else None
            run_cache_status(strategy=strategy)
        elif mode == "profiler":
            run_profiler_report()
        elif mode in ["btst", "swing", "gap"]:
            run_execution(strategy=mode.upper(), force_refresh=flags["force_refresh"])
            if _background_workers:
                should_wait = True
        elif mode == "report":
            run_report_only()
        elif mode == "invalidate":
            strategy = positionals[1].strip().upper() if len(positionals) > 1 else None
            DiscoveryCache().invalidate_cache(strategy=strategy)
        else:
            print(f"⚠️ Unknown mode '{mode}' at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.")
            print_help()

        if should_wait:
            print("Main process is waiting for background tasks to complete. Press CTRL+C to exit.")
            shutdown_manager.shutdown_event.wait()

    except KeyboardInterrupt:
        print("\nKeyboard interrupt received in main.py. Shutting down...")
    finally:
        if not shutdown_manager.is_shutdown():
            # If shutdown wasn't already initiated by a signal, start it now.
            shutdown_manager.initiate_shutdown()
        
        # Wait for the background workers to finish.
        if _background_workers:
            for worker in _background_workers:
                worker.join(timeout=10)

import os
import sys

# Add the project root to the Python path to resolve import issues
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- AUTO VENV ACTIVATION ---
from venv_activator import ensure_venv
ensure_venv()
# --------------------------

import threading
import subprocess
import ctypes
from datetime import datetime
import time
from dotenv import load_dotenv
from dataclasses import dataclass
from typing import Optional

import pandas as pd

import config
from data_broker import DataBroker
from discovery import execute_macro_discovery
from scanner_engine import HybridScanner
from profiler import profiler
from orchestrator import (
    run_fast_execution_scan,
    run_eod_scan,
)
from reporting import (
    format_terminal_table,
    run_report_pipeline,
    start_background_report,
    export_eod_pdf,
    fetch_gemini_news,
)
from cleaner import run_cleanup
from downloader import download_historical_constituents
from cache import DiscoveryCache, get_cached_discovery_row, load_last_signals, save_last_signals
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
    _, full_df, regime_analyzer = execute_macro_discovery(broker, scanner, strategy=strategy, force_refresh=force_refresh, cache=DiscoveryCache(), caller="Discovery")
    
    print("\n" + "="*80)
    print("🏆 TOP 10 SCANNED STOCKS RANKING (DISCOVERY PHASE)")
    df_slice = full_df.head(10)
    df_str = df_slice[['Symbol', 'Sector', 'RS_Pctl', 'ADX', 'Rank_Score']].to_string(index=False)
    print(format_terminal_table(df_str, df_slice))
    print("="*80 + "\n")
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
        execute_macro_discovery(broker, scanner, strategy=strategy, force_refresh=True, cache=DiscoveryCache(), caller="Discovery")[0]

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


def _print_top5(title: str, df: pd.DataFrame, score_col: str, cols: list):
    print(f"\n{'-'*80}\n🏆 TOP 5 {title}\n{'-'*80}")
    if df.empty:
        if df.attrs.get('phase1_unavailable'):
            print(
                "  (no signals - Phase 1 discovery cache was unavailable/stale, so this "
                "stage never actually scanned candidates today. NOT the same as a quiet "
                "market. Run `python main.py discover <strategy>` to refresh, or check "
                "logs/prewarm_*.log for why today's Prewarm didn't.)"
            )
        else:
            print("  (no signals)")
        return
    ranked = df.sort_values(by=score_col, ascending=False) if score_col in df.columns else df
    slice_df = ranked[[c for c in cols if c in ranked.columns]].head(5)
    print(slice_df.to_string(index=False))


def _notify_windows(title: str, message: str) -> None:
    """
    Fire-and-forget Windows balloon notification via PowerShell's built-in
    .NET Forms (System.Windows.Forms.NotifyIcon) - deliberately NOT a
    MessageBox, which blocks until someone clicks OK and would leave an
    unattended scheduled run hung indefinitely with no one there to dismiss it.
    No extra pip dependency: PowerShell + .NET Forms ships with Windows.
    """
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(15000, '{title}', '{message}', [System.Windows.Forms.ToolTipIcon]::Info); "
        "Start-Sleep -Seconds 16; "
        "$n.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            timeout=25, capture_output=True,
        )
    except Exception as e:
        print(f"⚠️ Windows notification failed (non-fatal): {e}")


def _combined_top_symbols(results: dict, top_n: int = 10) -> list:
    """Top symbols across all three eod strategies, deduped (preserving order),
    for the background Gemini news fetch below."""
    score_cols = {'BTST': 'BTST_Final_Score', 'SWING': 'Decision_Score', 'EMFB': 'EMFB_Score'}
    symbols: list = []
    for strategy, score_col in score_cols.items():
        df = results.get(strategy, pd.DataFrame())
        if df.empty or 'Symbol' not in df.columns:
            continue
        ranked = df.sort_values(by=score_col, ascending=False) if score_col in df.columns else df
        symbols.extend(ranked['Symbol'].head(5).tolist())
    return list(dict.fromkeys(symbols))[:top_n]


def _run_eod_news_enhancement(results: dict, base_pdf_filename: str) -> None:
    """
    Background-only companion to run_eod()'s fast PDF: fetches Gemini news for
    the eod picks and saves a second, news-enriched PDF. Deliberately NOT part
    of the synchronous fast path - see export_eod_pdf's docstring for why a
    live LLM call can't sit in front of the deadline-critical first PDF.
    """
    symbols = _combined_top_symbols(results)
    if not symbols:
        return
    try:
        news_summary = fetch_gemini_news(symbols)
    except Exception as e:
        print(f"⚠️ Background Gemini news fetch failed (non-fatal): {e}")
        return

    news_pdf_filename = base_pdf_filename.replace('.pdf', '_news.pdf')
    if export_eod_pdf(results, news_pdf_filename, news_summary=news_summary):
        _notify_windows("EOD News Added", f"News-enriched report saved: {news_pdf_filename}")
        print(f"📰 EOD news-enriched report saved to {os.path.abspath(news_pdf_filename)}")


def run_eod():
    """Runs BTST, SWING and EMFB sequentially and prints one consolidated Top 5 report."""
    from lifecycle_manager import shutdown_manager
    profiler.reset()
    print(f"🌇 EOD scan starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}...")
    try:
        broker, scanner = build_runtime()
    except Exception as e:
        print(f"❌ Could not start a broker session: {e}")
        print("Run `python main.py status` to diagnose credentials/connectivity before retrying `eod`.")
        profiler.save_report()
        profiler.print_report()
        return {}

    results = run_eod_scan(broker, scanner, top_n=config.Discovery.TOP_N_FAST_SCAN)

    is_post_close = not (
        datetime.strptime("09:15", "%H:%M").time()
        <= datetime.now(config.MARKET_TZ).time()
        <= datetime.strptime("15:30", "%H:%M").time()
    )
    if is_post_close:
        results['_stale_run_warning'] = (
            "This eod run started outside NSE market hours (09:15-15:30 IST). "
            "Phase 2 confirmation still fetches 'live' 15-min candles, which are "
            "stale/closed at this hour, so picks above may not reflect a real "
            "intraday opportunity - do not treat this the same as the scheduled "
            "15:15 run."
        )
        print(f"\n⚠️ {results['_stale_run_warning']}")

    print("\n" + "=" * 80)
    print("📊 END-OF-DAY CONSOLIDATED REPORT")
    print("=" * 80)

    _print_top5(
        "BTST PICKS", results.get('BTST', pd.DataFrame()), 'BTST_Final_Score',
        ['Symbol', 'Sector', 'BTST_Final_Score', 'Strength', 'Trigger', 'Stop', 'Target', 'Risk_Reward', 'Data_Stale'],
    )
    _print_top5(
        "SWING PICKS", results.get('SWING', pd.DataFrame()), 'Decision_Score',
        ['Symbol', 'Sector', 'Decision_Score', 'Strength', 'Trigger', 'Stop', 'Target', 'Risk_Reward', 'Data_Stale'],
    )
    _print_top5(
        "EMERGING MOMENTUM (EMFB) PICKS", results.get('EMFB', pd.DataFrame()), 'EMFB_Score',
        ['Symbol', 'Sector', 'EMFB_Score', 'Confidence', 'Trigger', 'Stop', 'Target', 'Data_Stale'],
    )

    stage_errors = results.get('_errors') or {}
    if stage_errors:
        print(f"\n{'-'*80}\n⚠️  STAGE FAILURES (results above reflect only the stages that succeeded)\n{'-'*80}")
        for stage, err in stage_errors.items():
            print(f"  {stage}: {err}")

    # Google Sheets logging (2026-08-25) - append today's BTST/SWING/EMFB
    # picks as rows to the shared sheet (see sheets_logger.py) so history
    # accumulates day over day for later trend analysis. Alert-only, same
    # pattern as the momentum-drop check below - a Sheets/network hiccup
    # must never block or fail the EOD report itself.
    try:
        from sheets_logger import log_scan_results

        def _df_to_sheet_rows(df: pd.DataFrame, category: str, score_col: str) -> list:
            rows = []
            for _, row in df.iterrows():
                rows.append({
                    "ticker": row.get('Symbol', ''),
                    "category": category,
                    "price": row.get('Trigger', ''),
                    "volume": row.get('Volume', ''),
                    "score": row.get(score_col, ''),
                    "notes": row.get('Reason', row.get('Strength', '')),
                })
            return rows

        sheet_rows = (
            _df_to_sheet_rows(results.get('BTST', pd.DataFrame()), 'btst', 'BTST_Final_Score')
            + _df_to_sheet_rows(results.get('SWING', pd.DataFrame()), 'swing', 'Decision_Score')
            + _df_to_sheet_rows(results.get('EMFB', pd.DataFrame()), 'emfb', 'EMFB_Score')
        )
        log_scan_results(sheet_rows)
    except Exception as e:
        print(f"\n⚠️ Sheets logging skipped due to an error: {e}")

    # Momentum-drop alert for user-held positions (watchlist_positions.yaml).
    # Alert-only, never blocks the EOD report if it fails - see
    # momentum_alert.py's docstring for the design rationale.
    try:
        from momentum_alert import check_momentum_drops
        alerts = check_momentum_drops(broker)
        if not alerts.empty:
            print(f"\n{'-'*80}\n🚨 MOMENTUM DROP ALERT (EMA20 broken on expanding volume)\n{'-'*80}")
            print(alerts.to_string(index=False))
            print(f"{'-'*80}")
    except Exception as e:
        print(f"\n⚠️ Momentum alert check skipped due to an error: {e}")

    print("=" * 80 + "\n")

    # Unattended-run support: eod previously only printed to the terminal, so a
    # scheduled/background run with no one watching would lose the output the
    # moment the window closed. Save a PDF and pop a non-blocking notification
    # so results are checkable later regardless of whether anyone was present.
    pdf_filename = f"eod_report_{datetime.now().strftime('%Y_%m_%d_%H%M')}.pdf"
    pdf_saved = export_eod_pdf(results, pdf_filename)
    if pdf_saved:
        pdf_path = os.path.abspath(pdf_filename)
        notify_title = "EOD Scan Complete (POST-CLOSE - STALE)" if is_post_close else "EOD Scan Complete"
        _notify_windows(notify_title, f"Report saved: {pdf_filename}")
        print(f"📄 EOD report saved to {pdf_path}")

        # eod mode has no should_wait/shutdown_event.wait() like btst/swing do,
        # so a daemon thread here would otherwise get killed the instant this
        # function returns and the process exits. Joining with a bounded
        # timeout keeps the process alive just long enough for the slower news
        # PDF to finish, WITHOUT delaying the fast PDF/notification above,
        # which already happened - and without hanging indefinitely if Gemini
        # is slow or down.
        news_thread = threading.Thread(
            target=_run_eod_news_enhancement, args=(results, pdf_filename),
            name="EODNewsEnhancement", daemon=True,
        )
        news_thread.start()
        shutdown_manager.register_worker(news_thread)
        news_thread.join(timeout=60)

    profiler.save_report()
    profiler.print_report()
    return results


def run_report_only():
    payload = load_last_signals()
    if not payload:
        print("⚠️ No saved signal set found. Run `python main.py btst` or `python main.py swing` first.")
        return
    created_at = payload.get("created_at", "unknown time")
    df_signals = payload.get("signals")

    # --- ADD THIS TYPE GUARD ---
    if not isinstance(df_signals, pd.DataFrame):
        print("⚠️ Saved signals are corrupted or missing.")
        return
    # ---------------------------

    print(f"📄 Building report from last saved signals ({created_at})...")
    run_report_pipeline(df_signals, strategy="report")


def run_live_scanner(strategy: str = "INTRADAY"):
    """Runs the fast execution scanner in a continuous loop."""
    profiler.reset()
    print(f"🚀 Starting LIVE scanner for {strategy.upper()} setups...")
    print("This will run the fast scanner in a loop. Press CTRL+C to exit.")
    broker, scanner = build_runtime()
    
    # --- NEW: Connect WebSocket ---
    broker.connect_websocket()
    print("Giving WebSocket time to connect...")
    time.sleep(5) # Allow a few seconds for the connection to establish
    # ----------------------------

    from emfb import run_emfb_scan, _is_scan_window

    cache = DiscoveryCache()
    discovery_thread: Optional[threading.Thread] = None
    last_discovery_time = datetime.min
    emfb_thread: Optional[threading.Thread] = None
    last_emfb_prewarm_date = None

    # Initial discovery run to ensure cache is populated
    print(f"♻️ Performing initial discovery scan for {strategy.upper()}...")
    watchlist, _, _ = execute_macro_discovery(broker, scanner, strategy=strategy, force_refresh=True, cache=cache, caller="LiveScannerInit")
    if watchlist:
        broker.subscribe_to_symbols(watchlist)

    last_discovery_time = datetime.now()

    while not shutdown_manager.is_shutdown():
        now = datetime.now()
        is_time_to_refresh = (now - last_discovery_time).total_seconds() > (config.LiveScanner.DISCOVERY_REFRESH_MINUTES * 60)

        # 1. Refresh discovery cache periodically in the background
        if is_time_to_refresh and (discovery_thread is None or not discovery_thread.is_alive()):
            print(f"♻️ Live scanner is refreshing discovery cache for {strategy.upper()} in the background...")

            def discovery_and_subscribe():
                """Worker function to refresh discovery and update subscriptions."""
                new_watchlist, _, _ = execute_macro_discovery(broker, scanner, strategy=strategy, force_refresh=True, cache=cache, caller="LiveScannerBG")
                if new_watchlist:
                    broker.subscribe_to_symbols(new_watchlist)
            discovery_thread = threading.Thread(target=discovery_and_subscribe, daemon=True, name="BackgroundDiscovery")

            discovery_thread.start()
            last_discovery_time = now # Reset timer as soon as we kick off the refresh

            if shutdown_manager.is_shutdown(): break

        # 1b. Pre-warm EMFB's own result cache once per day, the moment its scan
        # window opens (14:30) - so that a later one-off `python main.py eod`
        # (run as a separate command, possibly close to market close) hits
        # EMFB's cache instantly instead of re-fetching its full universe of
        # intraday candles, which is the single biggest cost in an `eod` run.
        # run_emfb_scan() is itself idempotent/cache-aware (see emfb.py), so
        # this is just "trigger it early" - correctness doesn't depend on this
        # firing at exactly 14:30, only on it firing before you run `eod`.
        if _is_scan_window() and last_emfb_prewarm_date != now.date() and (emfb_thread is None or not emfb_thread.is_alive()):
            print("♻️ Live scanner is pre-warming the EMFB cache in the background...")
            emfb_thread = threading.Thread(target=lambda: run_emfb_scan(broker=broker), daemon=True, name="BackgroundEMFBPrewarm")
            emfb_thread.start()
            last_emfb_prewarm_date = now.date()

            if shutdown_manager.is_shutdown(): break

        # 2. Run the fast execution scan
        # This function already clears the screen and prints a clean report.
        run_fast_execution_scan(
            broker, 
            scanner, 
            strategy=strategy, 
            top_n=config.Discovery.TOP_N_FAST_SCAN, 
            display=True, 
            force_refresh=False, # Use the cache
            caller="LiveScanner"
        )
        
        if shutdown_manager.is_shutdown(): break

        # 3. Wait for the next interval, but be responsive to shutdown.
        print(f"\nNext scan in {config.LiveScanner.LOOP_INTERVAL_SECONDS} seconds. Press CTRL+C to exit.")
        if shutdown_manager.shutdown_event.wait(timeout=config.LiveScanner.LOOP_INTERVAL_SECONDS):
            break

    print("\nLive scanner shut down.")

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
    from reporting import fetch_gemini_news
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
    close = float(metrics.get("Close") or 0)
    ema50 = float(metrics.get("EMA50") or close)
    rsi = float(metrics.get("RSI") or 50)
    liquidity = float(metrics.get("Avg_Traded_Value_20d") or 0)

    if (liquidity is not None and liquidity <= config.HoldingRecommendation.MIN_LIQUIDITY_RUPEES
            or close < ema50
            or rsi < 50
            or rs_percentile < config.HoldingRecommendation.MIN_RS_PERCENTILE):
        return "EXIT"

    if signal and decision_score >= config.HoldingRecommendation.MIN_DECISION_SCORE_FOR_HOLD:
        return "HOLD"

    return "WATCH"


def explain_decision_score(row: pd.Series):
    """Explains the unified 'cooled' Decision_Score calculation from utils.py."""
    score_val = float(row.get("Score") or 0)
    rs_val = float(row.get("RS_Pctl") or 0)
    sector_val = float(row.get("Sector_RS") or 0)
    adx_val = float(row.get("ADX") or 0)

    # Cooled (inverted) values - Sector_RS is deliberately NOT cooled (see utils.py's
    # add_decision_scores for why: sector rotation persists rather than mean-reverts).
    score_cool = 100 - score_val
    rs_cool = 100 - rs_val
    adx_cool = 100 - (min(adx_val, 50.0) / 50.0 * 100)

    # Weighted components
    rs_part = rs_cool * 0.40
    score_part = score_cool * 0.35
    sector_part = sector_val * 0.15
    adx_part = adx_cool * 0.10
    total = rs_part + score_part + sector_part + adx_part

    print("\nDecision Score Explanation (Favors less-crowded setups, but follows sector rotation)")
    print(f"  RS Pctl ({rs_val:.1f}):        (100 - {rs_val:.1f}) * 40% = {rs_part:.1f}")
    print(f"  Scanner Score ({score_val:.1f}):  (100 - {score_val:.1f}) * 35% = {score_part:.1f}")
    print(f"  Sector RS ({sector_val:.1f}):    {sector_val:.1f} * 15% = {sector_part:.1f}")
    print(f"  ADX ({adx_val:.1f}):            (100 - {min(adx_val, 50.0):.1f}/50*100) * 10% = {adx_part:.1f}")
    print("-" * 50)
    print(f"  Final Decision Score:  {total:.1f}")


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

@dataclass
class AnalysisData:
    """A container for all data required for a single-stock analysis."""
    df_daily: pd.DataFrame
    daily_metrics: dict
    df_15min: pd.DataFrame
    nifty_df: pd.DataFrame
    rs_percentile: float
    sector_rs: float
    rs_source: str


def _get_analysis_data(broker: DataBroker, scanner: HybridScanner, symbol: str, force_refresh: bool) -> Optional[AnalysisData]:
    """
    Fetches all required data for a single-stock analysis, prioritizing cache.

    Returns an AnalysisData object on success, or None if essential data is missing.
    """
    caller_context = "Analyze"

    # 1. Attempt to load daily data and pre-computed metrics from discovery cache
    cached_row, rs_source = None, "neutral default"
    if not force_refresh:
        cached_row, rs_source = get_cached_discovery_row(symbol)
        if rs_source is None:
            # On cache miss, get_cached_discovery_row returns (None, None).
            # The RS values will use the neutral default of 50.0, so the source should also be the neutral default.
            rs_source = "neutral default"

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
        return None
    if df_15min.empty:
        print(f"⚠️ No 15-minute candles found for {symbol}.")
        return None

    return AnalysisData(
        df_daily=df_daily,
        daily_metrics=daily_metrics,
        df_15min=df_15min,
        nifty_df=nifty_df,
        rs_percentile=rs_percentile,
        sector_rs=sector_rs,
        rs_source=rs_source,
    )

def _build_analysis_report(symbol: str, signal: Optional[dict], daily_metrics: dict, df_15min: pd.DataFrame, rs_percentile: float, sector_rs: float, rs_source: str, holding: bool) -> tuple[dict, pd.DataFrame]:
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


def run_analyze(symbol: str, explain: bool = False, holding: bool = False, force_refresh: bool = False, checksheet: bool = False):
    profiler.reset()
    symbol = symbol.strip().upper()
    if not symbol:
        print("⚠️ Please provide a symbol. Example: python main.py analyze HAL")
        return pd.DataFrame()

    print(f"🔎 Analyzing {symbol} only...")
    broker, scanner = build_runtime()

    # 1. Fetch all data
    analysis_data = _get_analysis_data(
        broker, scanner, symbol, force_refresh
    )

    if analysis_data is None:
        profiler.save_report()
        profiler.print_report()
        return pd.DataFrame()

    # 2. Run the final analysis with the combined data.
    regime = scanner.compute_market_regime(analysis_data.nifty_df)
    # This path always fetches df_15min live (see _get_analysis_data), so - unlike
    # orchestrator.py's backtest path - there's no point_in_time to guard against here.
    live_close = float(analysis_data.df_15min['Close'].iloc[-1]) if not analysis_data.df_15min.empty else None
    signal = scanner.scan(
        symbol,
        analysis_data.df_daily,
        analysis_data.df_15min,
        rs_percentile=analysis_data.rs_percentile,
        sector_rs=analysis_data.sector_rs,
        regime_mult=regime["multiplier"],
        strategy="SWING",
        daily_metrics=analysis_data.daily_metrics,
        live_close=live_close,
    )

    # 3. Build and print the report
    report_data, ranked_df = _build_analysis_report(
        symbol, signal, analysis_data.daily_metrics, analysis_data.df_15min, analysis_data.rs_percentile, analysis_data.sector_rs, analysis_data.rs_source, holding
    )
    _print_analysis_report(report_data)

    # 3b. Recent Corporate Catalysts (NSE Disclosures + Gemini AI)
    try:
        from nse_announcements import fetch_symbol_catalysts
        catalysts = fetch_symbol_catalysts(symbol, lookback_days=7, use_ai=True)
        if catalysts:
            print("\n📰 RECENT CORPORATE CATALYSTS (NSE Disclosures + Gemini AI)")
            sentiment_icons = {'BULLISH': '🟢', 'BEARISH': '🔴', 'NEUTRAL': '⚪'}
            for cat in catalysts[:3]:
                score = cat.get('AI_Catalyst_Score')
                sent = cat.get('AI_Sentiment', 'NEUTRAL')
                icon = sentiment_icons.get(sent, '⚪')
                mag = cat.get('AI_Magnitude', 'MODERATE')
                score_str = f"Score {score:.1f}/10 ({mag})" if score else cat.get('Materiality', 'MEDIUM')
                takeaway = cat.get('AI_Takeaway') or cat.get('Summary', '')[:120]
                print(f"  {icon} [{score_str}] {cat.get('Category', '')}: {takeaway}")
            print("-" * 80)
    except Exception as e:
        logger.debug(f"Symbol catalyst lookup skipped: {e}")

    # 3c. Multimodal Visual Chart AI (Minervini VCP & Base Geometry)
    try:
        from chart_ai import analyze_symbol_chart
        chart_res = analyze_symbol_chart(analysis_data.df_daily, symbol, save_image=True)
        if chart_res and chart_res.get('pattern_type') not in ['NO_DATA', 'UNKNOWN']:
            v_score = chart_res.get('visual_quality_score', 5.0)
            verdict = chart_res.get('visual_verdict', 'DEVELOPING_BASE')
            pattern = chart_res.get('pattern_type', 'UNKNOWN')
            vcp = chart_res.get('vcp_contractions', 'N/A')
            vol = chart_res.get('volume_signature', 'CHOPPY')
            pivot = chart_res.get('key_pivot_price')
            takeaway = chart_res.get('one_line_takeaway', '')
            chart_path = chart_res.get('chart_path')

            verdict_icon = '🟢' if 'PRIME' in verdict else ('🟡' if 'DEVELOPING' in verdict else '🔴')
            print("\n🎨 GEMINI 2.5 VISUAL CHART AI (Minervini VCP & Base Geometry)")
            print(f"  {verdict_icon} Visual Quality Score:  {v_score:.1f} / 10.0  ({verdict})")
            print(f"  📐 Base Pattern:           {pattern}")
            print(f"  🔄 VCP Contractions:       {vcp}")
            print(f"  📊 Volume Signature:       {vol}")
            if pivot:
                print(f"  🎯 Key Pivot Breakout:     ₹{pivot:.2f}")
            print(f"  💡 Visual Rationale:       {takeaway}")
            if chart_path:
                print(f"  🖼️  Chart Saved:            {chart_path}")
            print("-" * 80)
    except Exception as e:
        logger.debug(f"Visual Chart AI analysis skipped: {e}")

    # 4. Handle optional flags and cleanup
    if explain and not ranked_df.empty:
        explain_decision_score(ranked_df.iloc[0])

    if checksheet:
        from check_sheet_logger import CheckSheetLogger
        CheckSheetLogger().evaluate_and_log(
            symbol=symbol,
            signal=signal,
            daily_metrics=analysis_data.daily_metrics,
            df_15min=analysis_data.df_15min,
            df_daily=analysis_data.df_daily,
            nifty_df=analysis_data.nifty_df,
            rs_percentile=analysis_data.rs_percentile,
            sector_rs=analysis_data.sector_rs,
            regime_mult=regime["multiplier"],
            strategy="SWING",
            display=True,
            persist_db=True,
            save_file=True,
        )

    profiler.save_report()
    profiler.print_report()
    return ranked_df


def run_check_sheet(symbol: str, strategy: str = "SWING", force_refresh: bool = False, explain: bool = False):
    """
    Evaluates and displays a systematic 6-pillar Check Sheet for a single ticker,
    persists the audit log into SQLite (check_sheet_logs table), and exports JSON.
    """
    profiler.reset()
    symbol = symbol.strip().upper()
    strategy = strategy.strip().upper()
    if not symbol:
        print("⚠️ Please provide a symbol. Example: python main.py check-sheet HAL")
        return None

    print(f"📋 Generating {strategy} Check Sheet for {symbol}...")
    broker, scanner = build_runtime()

    analysis_data = _get_analysis_data(broker, scanner, symbol, force_refresh)
    if analysis_data is None:
        print(f"⚠️ Could not assemble necessary candle and market data for {symbol}.")
        profiler.save_report()
        profiler.print_report()
        return None

    regime = scanner.compute_market_regime(analysis_data.nifty_df)
    live_close = float(analysis_data.df_15min['Close'].iloc[-1]) if not analysis_data.df_15min.empty else None
    signal = scanner.scan(
        symbol,
        analysis_data.df_daily,
        analysis_data.df_15min,
        rs_percentile=analysis_data.rs_percentile,
        sector_rs=analysis_data.sector_rs,
        regime_mult=regime["multiplier"],
        strategy=strategy,
        daily_metrics=analysis_data.daily_metrics,
        live_close=live_close,
    )

    from check_sheet_logger import CheckSheetLogger
    cs_logger = CheckSheetLogger()
    check_sheet = cs_logger.evaluate_and_log(
        symbol=symbol,
        signal=signal,
        daily_metrics=analysis_data.daily_metrics,
        df_15min=analysis_data.df_15min,
        df_daily=analysis_data.df_daily,
        nifty_df=analysis_data.nifty_df,
        rs_percentile=analysis_data.rs_percentile,
        sector_rs=analysis_data.sector_rs,
        regime_mult=regime["multiplier"],
        market_regime_info=regime,
        strategy=strategy,
        display=True,
        persist_db=True,
        save_file=True,
    )

    if explain and signal:
        ranked_df = add_decision_scores(pd.DataFrame([signal]))
        if not ranked_df.empty:
            explain_decision_score(ranked_df.iloc[0])

    profiler.save_report()
    profiler.print_report()
    return check_sheet


def print_single_cache_status(strategy: Optional[str] = None):
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


def run_cache_status(strategy: Optional[str] = None):
    if strategy:
        print_single_cache_status(strategy=strategy)
        return

    for index, strategy_name in enumerate(["BTST", "SWING", "GAP", "INTRADAY"]):
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
    print("  python main.py analyze SYMBOL [--explain] [--holding] [--checksheet]  # analyze one stock only")
    print("  python main.py check-sheet SYMBOL [SWING|BTST|EMFB]          # evaluate and log 6-pillar trade setup check sheet")
    print("  python main.py discover [BTST|SWING|GAP|INTRADAY] [--force-refresh]   # refresh discovery cache only")
    print("  python main.py cache [BTST|SWING|GAP|INTRADAY]               # show discovery cache metadata")
    print("  python main.py profiler                                      # show the last run's API profiler report")
    print("  python main.py btst                                          # fast BTST scan from cached discovery")
    print("  python main.py swing                                         # fast SWING scan from cached discovery")
    print("  python main.py gap                                           # fast GAP scan from cached discovery")
    print("  python main.py intraday                                      # fast INTRADAY scan from cached discovery")
    print("  python main.py live [INTRADAY|BTST]                          # run a continuous live scan loop")
    print("  python main.py btst --force-refresh                          # rebuild BTST cache, then scan")
    print("  python main.py report                                        # create report from last displayed signals")
    print("  python main.py download-constituents [nifty500]              # Download historical index members to data/ folder")
    print("  python main.py clean-constituents [nifty500]                 # De-duplicate and prune historical constituent files")
    print(
        "  python main.py emfb [--force-refresh]                        # run Emerging Momentum/Fresh Breakout scan (cached ~1hr; --force-refresh bypasses)"
    )
    print(
        "  python main.py momentum [--force-refresh]                    # run always-on momentum scan (no regime gate; cached per momentum_config.yaml; --force-refresh bypasses)"
    )
    print(
        "  python main.py trigger-status [emfb|momentum|<path.csv>]     # check an existing report's Trigger/Stop/Target against live prices since it ran"
    )
    print(
        "  python main.py history SYMBOL [emfb|momentum]                # show a symbol's score/rank history across all past reports"
    )
    print(
        "  python main.py watchlist [emfb|momentum]                     # diff the two most recent reports: new / dropped / continuing symbols"
    )
    print(
        "  python main.py scorecard [emfb|momentum]                     # win/loss track record across every past report (hit target vs stopped out)"
    )
    print(
        "  python main.py actionable [emfb|momentum]                    # relabel the latest report's Confidence tiers by their PROVEN win rate (run scorecard first)"
    )
    print(
        "  python main.py extension [emfb|momentum]                     # flag signals that were already overextended (far above EMA20) at signal time"
    )
    print(
        "  python main.py news [emfb|momentum] [--ai]                   # NSE corporate announcements; --ai enables Gemini Tier-2 catalyst scoring"
    )
    print(
        "  python main.py fitness                                       # score the universe for SHORT-TERM trading fitness (liquidity/F&O/ATR-range/trend), tag BTST/SWING-eligible"
    )
    print(
        "  python main.py expand                                        # score external F&O names (not in universe) on the same fitness criteria; propose add/replace swaps"
    )
    print(
        "  python main.py stockscan [momentum|emfb]                     # MASTER: scan -> fitness -> actionable -> extension -> catalyst; decision shortlist before 15:20"
    )
    print(
        "  python main.py surge                                         # market-wide volume+price surge scan (whole bhavcopy universe); flags NEEDS_INVESTIGATION for Claude to web-search"
    )
    print(
        "  python main.py gapscan                                       # today's open vs prior close, curated universe; run near 09:15, ranks gap-up/gap-down"
    )
    print(
        "  python main.py reliability                                   # per-symbol win rate/avg return from resolved trade history; flags real repeated winners/losers"
    )
    print(
        "  python main.py grind                                         # multi-week steady movers the 1-day RS ranking misses (reads cache, no API calls)",
        "  python main.py brief                                         # ONE COMMAND: BTST / SWING / GRIND candidates, stage- and reliability-filtered (reads cache, no API calls)"
    )
    print("  python main.py eod [--force-refresh]                         # run BTST+SWING+EMFB, print consolidated Top 5 report")
    print("  python main.py invalidate [BTST|SWING|GAP|INTRADAY]          # delete discovery cache")
    print("  python main.py refresh-beta [--force-refresh]                # recompute BETA_REGISTRY from live rolling beta (once/day; --force-refresh ignores that)")

def parse_cli(argv):
    known_flags = {"--force-refresh", "--explain", "--holding", "--ai", "--checksheet"}
    flags = {
        "force_refresh": "--force-refresh" in argv,
        "explain": "--explain" in argv,
        "holding": "--holding" in argv,
        "ai": "--ai" in argv,
        "checksheet": "--checksheet" in argv,
    }
    positionals = [arg for arg in argv if not arg.startswith("--")]
    unknown_flags = [arg for arg in argv if arg.startswith("--") and arg not in known_flags]
    return positionals, flags, unknown_flags


INSTANCE_LOCK_PATH = os.path.join(project_root, "main.pid")


def _pid_is_alive(pid: int) -> bool:
    """Windows-safe liveness check - avoids adding a psutil dependency just for this.

    OpenProcess returning a valid handle only means the process *object* still
    exists, which can briefly remain true even after the process has exited
    (e.g. while some other handle to it is still open) - so a successful
    OpenProcess alone is not sufficient. GetExitCodeProcess must also be
    checked against STILL_ACTIVE to confirm it's actually still running.

    Also note: OpenProcess returns a 64-bit HANDLE; ctypes defaults an
    undeclared restype to 32-bit c_int, which truncates the handle and can
    make a live process read back as a null (dead) handle. restype/argtypes
    must be set explicitly.
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _acquire_instance_lock() -> None:
    """
    Ensures only one main.py process runs at a time, regardless of how it's
    invoked (Task Scheduler, a manual terminal, or anything else) - see the
    2026-07-31 incident where three concurrent main.py processes each paced
    their own API calls independently, multiplying the real request rate
    against AngelOne's rate limit. Must run before any other work (no API
    calls, no scan) so a duplicate invocation is genuinely a no-op.
    """
    if os.path.exists(INSTANCE_LOCK_PATH):
        try:
            with open(INSTANCE_LOCK_PATH, "r") as f:
                existing_pid = int(f.read().strip())
        except (ValueError, OSError):
            existing_pid = None

        if existing_pid is not None and _pid_is_alive(existing_pid):
            print(f"Another instance is already running (PID {existing_pid}), exiting.")
            sys.exit(1)
        # Stale lock (owning process is gone, or the file was unreadable) - fall through and overwrite it.

    with open(INSTANCE_LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))

    def _release_instance_lock():
        try:
            os.remove(INSTANCE_LOCK_PATH)
        except OSError:
            pass

    shutdown_manager.register(_release_instance_lock)


if __name__ == "__main__":
    from lifecycle_manager import shutdown_manager

    _acquire_instance_lock()

    positionals, flags, unknown_flags = parse_cli(sys.argv[1:])
    for flag in unknown_flags:
        print(f"⚠️ Unknown option '{flag}' ignored.")

    mode = positionals[0].strip().lower() if positionals else "btst"
    should_wait = False

    try:
        if mode == "status":
            run_system_check()
        elif mode in ["check-sheet", "checksheet", "check_sheet"]:
            symbol = positionals[1] if len(positionals) > 1 else ""
            strategy = positionals[2].strip().upper() if len(positionals) > 2 else "SWING"
            run_check_sheet(symbol, strategy=strategy, force_refresh=flags["force_refresh"], explain=flags["explain"])
        elif mode == "download-constituents":
            index_name = "nifty500"
            if len(positionals) > 1:
                index_name = positionals[1].strip().lower().replace(" ", "")
            download_historical_constituents(index_name=index_name)
        elif mode == "clean-constituents":
            index_name = positionals[1].strip().lower().replace(" ", "") if len(positionals) > 1 else None
            run_cleanup(index_name=index_name)
        elif mode == "refresh-beta":
            broker = DataBroker()
            updated = broker.refresh_beta_registry(force=flags["force_refresh"])
            print(f"✅ Refreshed BETA_REGISTRY for {updated} symbols." if updated else "ℹ️ BETA_REGISTRY refresh skipped (already run today) or failed - see logs.")
            broker.close_session()
        elif mode == "discover":
            strategy = positionals[1].strip().upper() if len(positionals) > 1 else "BTST"
            run_discovery(strategy=strategy, force_refresh=flags["force_refresh"])
        elif mode == "analyze":
            symbol = positionals[1] if len(positionals) > 1 else ""
            run_analyze(symbol, explain=flags["explain"], holding=flags["holding"], force_refresh=flags["force_refresh"], checksheet=flags["checksheet"])
        elif mode == "cache":
            strategy = positionals[1].strip().upper() if len(positionals) > 1 else None
            run_cache_status(strategy=strategy)
        elif mode == "profiler":
            run_profiler_report()
        elif mode in ["btst", "swing", "gap", "intraday"]:
            run_execution(strategy=mode.upper(), force_refresh=flags["force_refresh"])
            should_wait = True
        elif mode == "emfb":
            from emfb import run_emfb_scan
            run_emfb_scan(force_refresh=flags["force_refresh"])
        elif mode == "momentum":
            from momentum_scanner import run_momentum_scan
            run_momentum_scan(force_refresh=flags["force_refresh"])
        elif mode == "trigger-status":
            from trigger_status import find_latest_report, check_trigger_status, print_trigger_status
            report_type = positionals[1].strip().lower() if len(positionals) > 1 else "emfb"
            if report_type in ("emfb", "momentum"):
                report_path = find_latest_report(report_type)
                if report_path is None:
                    print(f"⚠️ No {report_type} report files found.")
                else:
                    print_trigger_status(check_trigger_status(report_path))
            else:
                # Treat the argument as an explicit file path instead of a report type.
                print_trigger_status(check_trigger_status(positionals[1]))
        elif mode == "history":
            from symbol_history import symbol_history, print_symbol_history
            symbol = positionals[1] if len(positionals) > 1 else ""
            report_type = positionals[2].strip().lower() if len(positionals) > 2 else "emfb"
            print_symbol_history(symbol, symbol_history(symbol, report_type))
        elif mode == "watchlist":
            from symbol_history import watchlist_diff, print_watchlist_diff
            report_type = positionals[1].strip().lower() if len(positionals) > 1 else "emfb"
            print_watchlist_diff(watchlist_diff(report_type))
        elif mode == "scorecard":
            from performance_tracker import build_scorecard, summarize_scorecard, print_scorecard, save_tier_performance
            report_type = positionals[1].strip().lower() if len(positionals) > 1 else "emfb"
            scorecard = build_scorecard(report_type)
            summary = summarize_scorecard(scorecard)
            print_scorecard(scorecard, summary)
            save_tier_performance(report_type, summary)
        elif mode == "actionable":
            from trigger_status import find_latest_report
            from signal_quality import annotate_actionability, print_actionable_report
            report_type = positionals[1].strip().lower() if len(positionals) > 1 else "emfb"
            report_path = find_latest_report(report_type)
            if report_path is None:
                print(f"⚠️ No {report_type} report files found.")
            else:
                import pandas as pd
                report_df = pd.read_csv(report_path)
                print_actionable_report(annotate_actionability(report_df, report_type))
        elif mode == "extension":
            from trigger_status import find_latest_report
            from extension_score import annotate_extension, print_extension_report
            report_type = positionals[1].strip().lower() if len(positionals) > 1 else "emfb"
            report_path = find_latest_report(report_type)
            if report_path is None:
                print(f"⚠️ No {report_type} report files found.")
            else:
                import pandas as pd
                report_df = pd.read_csv(report_path)
                print_extension_report(annotate_extension(report_df))
        elif mode == "news":
            from nse_announcements import build_watchlist, print_watchlist
            sub_pos = [p for p in positionals[1:] if p.lower() != "ai"]
            report_type = sub_pos[0].strip().lower() if sub_pos else "emfb"
            use_ai = flags.get("ai", False) or any(p.lower() == "ai" for p in positionals)
            watchlist = build_watchlist(report_type=report_type, use_ai=use_ai)
            print_watchlist(watchlist)
        elif mode == "fitness":
            from universe_fitness import build_universe_fitness, print_fitness_report
            print_fitness_report(build_universe_fitness())
        elif mode == "expand":
            from universe_expansion import build_expansion_candidates, print_expansion_report
            print_expansion_report(build_expansion_candidates())
        elif mode == "surge":
            from volume_surge_scanner import build_surge_report, print_surge_report
            print_surge_report(build_surge_report())
        elif mode == "gapscan":
            from gap_scanner import build_gap_report, print_gap_report
            print_gap_report(build_gap_report())
        elif mode == "reliability":
            from symbol_reliability import print_reliability_report
            print_reliability_report()
        elif mode == "grind":
            from grind_scanner import build_grind_report, print_grind_report, find_latest_momentum_report
            print_grind_report(build_grind_report(find_latest_momentum_report()))
        elif mode in ("brief", "decision-brief"):
            from decision_brief import run_decision_brief
            run_decision_brief()
        elif mode in ("stockscan", "stock-scan"):
            from stock_scan import run_stock_scan
            rtype = positionals[1].strip().lower() if len(positionals) > 1 else "momentum"
            run_stock_scan(report_type=rtype)
        elif mode == "eod":
            run_eod()
        elif mode == "live":
            strategy = positionals[1].strip().upper() if len(positionals) > 1 else "INTRADAY"
            run_live_scanner(strategy=strategy)
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

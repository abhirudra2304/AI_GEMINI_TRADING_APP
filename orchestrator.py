import os
import time
import logging
import sqlite3
import requests
import random
import pickle
import threading
from typing import Optional
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import config
from data_broker import DataBroker
from scanner_engine import HybridScanner
from utils import install_and_import, add_decision_scores
from lifecycle_manager import shutdown_manager

genai = install_and_import('google-generativeai', 'google.generativeai')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("Asia/Kolkata")
DEFAULT_DISCOVERY_CACHE_FILE = "discovery_cache.pkl"
STRATEGY_DISCOVERY_CACHE_FILES = {
    "BTST": "discovery_cache_btst.pkl",
    "SWING": "discovery_cache_swing.pkl",
    "GAP": "discovery_cache_gap.pkl",
    "SILENT": "discovery_cache_silent.pkl",
}
DEFAULT_LAST_SIGNALS_FILE = "last_signals.pkl"

# List to keep track of background worker threads
_background_workers = []

def join_background_workers():
    """Joins all registered background worker threads."""
    print("Stopping background workers...")
    logger.info("Waiting for background report generator to finish...")
    for worker in _background_workers:
        worker.join(timeout=30)  # Wait for 30 seconds max
        if worker.is_alive():
            logger.warning(f"Background worker {worker.name} did not terminate in time.")
    print("Background workers stopped.")
    logger.info("Background workers stopped.")

shutdown_manager.register(join_background_workers)


class DiscoveryCache:
    """Owns the Phase 1 discovery snapshot so Phase 2 never refetches daily data."""
    _memory_cache = {}

    def __init__(self, path: str = None, ttl_minutes: int = config.CacheConfig.CACHE_REFRESH_MINUTES):
        self._explicit_path = path
        self.path = path or DEFAULT_DISCOVERY_CACHE_FILE
        self.ttl = timedelta(minutes=ttl_minutes)
        self._last_load_error = None
        shutdown_manager.register(self.save_on_shutdown)

    def save_on_shutdown(self):
        """Placeholder for any cache-flushing logic if needed."""
        # This can be expanded if there are unsaved changes in memory cache.
        # For now, saving is explicit, but this is good practice.
        print("Saving cache...")
        logger.info("Flushing cache to disk...")
        pass


    @staticmethod
    def _normalize_strategy(strategy: str = None) -> str:
        return strategy.upper() if strategy else None

    def _cache_path_for_strategy(self, strategy: str = None) -> str:
        if self._explicit_path:
            return self._explicit_path
        normalized = self._normalize_strategy(strategy)
        return STRATEGY_DISCOVERY_CACHE_FILES.get(normalized, DEFAULT_DISCOVERY_CACHE_FILE)

    def _load_path_for_strategy(self, strategy: str = None) -> str:
        primary_path = self._cache_path_for_strategy(strategy)
        if self._explicit_path or os.path.exists(primary_path):
            return primary_path

        # Backward compatibility: accept the old shared cache if no strategy file exists yet.
        normalized = self._normalize_strategy(strategy)
        if normalized in STRATEGY_DISCOVERY_CACHE_FILES and os.path.exists(DEFAULT_DISCOVERY_CACHE_FILE):
            return DEFAULT_DISCOVERY_CACHE_FILE
        return primary_path

    @staticmethod
    def _is_market_hours(value: datetime) -> bool:
        market_time = value.astimezone(MARKET_TZ).time()
        return datetime.strptime("09:15", "%H:%M").time() <= market_time <= datetime.strptime("15:30", "%H:%M").time()

    def load_cache(self, strategy: str = None):
        self._last_load_error = None
        path = self._load_path_for_strategy(strategy)
        self.path = path
        if not os.path.exists(path):
            self._memory_cache.pop(path, None)
            return None
        cached = self._memory_cache.get(path)
        if cached is not None:
            return cached
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
            self._memory_cache[path] = payload
            return payload
        except Exception as e:
            self._last_load_error = str(e)
            logger.warning(f"Failed to load discovery cache: {e}")
            return None

    def save_cache(self, discovered_df: pd.DataFrame, watchlist: list, strategy: str, regime: dict):
        normalized_strategy = self._normalize_strategy(strategy) or "SWING"
        path = self._cache_path_for_strategy(normalized_strategy)
        self.path = path
        payload = {
            "created_at": datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
            "strategy": normalized_strategy,
            "ttl_minutes": int(self.ttl.total_seconds() // 60),
            "watchlist": watchlist,
            "regime": regime,
            "discovered_df": discovered_df,
        }
        with open(path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        self._memory_cache[path] = payload
        logger.info(f"✅ Discovery cache saved to {path} with {len(discovered_df)} ranked symbols.")
        return payload

    @staticmethod
    def _format_timedelta(value: timedelta) -> str:
        total_seconds = max(0, int(value.total_seconds()))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def get_cache_metadata(self, strategy: str = None) -> dict:
        now = datetime.now(MARKET_TZ)
        path = self._load_path_for_strategy(strategy)
        self.path = path
        metadata = {
            "path": path,
            "primary_path": self._cache_path_for_strategy(strategy),
            "legacy_fallback": path == DEFAULT_DISCOVERY_CACHE_FILE and self._normalize_strategy(strategy) in STRATEGY_DISCOVERY_CACHE_FILES,
            "exists": os.path.exists(path),
            "current_time": now,
            "generated_time": None,
            "age": None,
            "age_text": "N/A",
            "ttl": self.ttl,
            "ttl_text": self._format_timedelta(self.ttl),
            "remaining": None,
            "remaining_text": "N/A",
            "strategy": None,
            "expected_strategy": self._normalize_strategy(strategy),
            "stock_count": 0,
            "market_hours": self._is_market_hours(now),
            "valid": False,
            "invalid_reason": None,
            "load_error": None,
        }

        if not metadata["exists"]:
            metadata["invalid_reason"] = "missing file"
            return metadata

        payload = self.load_cache(strategy)
        if not payload:
            metadata["load_error"] = self._last_load_error
            metadata["invalid_reason"] = f"corrupted cache: {self._last_load_error}" if self._last_load_error else "empty cache payload"
            return metadata
        if not isinstance(payload, dict):
            metadata["invalid_reason"] = f"corrupted cache: expected dict payload, got {type(payload).__name__}"
            return metadata

        metadata["strategy"] = payload.get("strategy")
        try:
            created_at = datetime.fromisoformat(payload["created_at"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=MARKET_TZ)
        except Exception:
            metadata["invalid_reason"] = "corrupted cache: missing or invalid created_at timestamp"
            return metadata

        created_at = created_at.astimezone(MARKET_TZ)
        age = now - created_at
        remaining = self.ttl - age
        metadata["generated_time"] = created_at
        metadata["age"] = age
        metadata["age_text"] = self._format_timedelta(age)
        metadata["remaining"] = remaining
        metadata["remaining_text"] = self._format_timedelta(remaining)

        discovered_df = payload.get("discovered_df")
        if isinstance(discovered_df, pd.DataFrame):
            metadata["stock_count"] = len(discovered_df)

        if created_at.date() != now.date():
            metadata["invalid_reason"] = (
                f"wrong trading day: generated on {created_at.date()}, current market date is {now.date()}"
            )
            return metadata
        if age > self.ttl:
            metadata["invalid_reason"] = f"expired TTL: age {metadata['age_text']} exceeds TTL {metadata['ttl_text']}"
            return metadata
        if strategy and payload.get("strategy") != self._normalize_strategy(strategy):
            metadata["invalid_reason"] = (
                f"strategy mismatch: cache has {payload.get('strategy')}, requested {self._normalize_strategy(strategy)}"
            )
            return metadata
        if not isinstance(discovered_df, pd.DataFrame):
            metadata["invalid_reason"] = "corrupted cache: discovered_df is missing or not a DataFrame"
            return metadata
        if discovered_df.empty:
            metadata["invalid_reason"] = "empty discovery result: discovered_df has 0 rows"
            return metadata

        metadata["valid"] = True
        return metadata

    def log_cache_diagnostics(self, metadata: dict):
        logger.warning(
            "Discovery cache invalid (%s). Path=%s | Generated=%s | Current=%s | Age=%s | TTL=%s | Strategy=%s | Expected=%s | Stocks=%s",
            metadata.get("invalid_reason"),
            metadata.get("path"),
            metadata["generated_time"].isoformat(timespec="seconds") if metadata.get("generated_time") else "N/A",
            metadata["current_time"].isoformat(timespec="seconds") if metadata.get("current_time") else "N/A",
            metadata.get("age_text", "N/A"),
            metadata.get("ttl_text", "N/A"),
            metadata.get("strategy") or "N/A",
            metadata.get("expected_strategy") or "N/A",
            metadata.get("stock_count", 0),
        )

    def is_cache_valid(self, strategy: str = None) -> bool:
        metadata = self.get_cache_metadata(strategy)
        if not metadata["valid"]:
            self.log_cache_diagnostics(metadata)
        elif metadata.get("market_hours"):
            logger.info(
                "Discovery cache valid for %s during market hours. Path=%s | Generated=%s | Remaining=%s | Stocks=%s",
                metadata.get("expected_strategy") or metadata.get("strategy") or "ANY",
                metadata.get("path"),
                metadata["generated_time"].isoformat(timespec="seconds") if metadata.get("generated_time") else "N/A",
                metadata.get("remaining_text", "N/A"),
                metadata.get("stock_count", 0),
            )
        return metadata["valid"]

    def invalidate_cache(self, strategy: str = None):
        paths = [self._cache_path_for_strategy(strategy)] if strategy else [
            *STRATEGY_DISCOVERY_CACHE_FILES.values(),
            DEFAULT_DISCOVERY_CACHE_FILE,
        ]
        for path in dict.fromkeys(paths):
            self._memory_cache.pop(path, None)
            if os.path.exists(path):
                os.remove(path)
        logger.info("Discovery cache invalidated.")

def save_last_signals(df_signals: pd.DataFrame, path: str = DEFAULT_LAST_SIGNALS_FILE):
    """Persists the latest displayed signals so `python main.py report` can run separately."""
    payload = {
        "created_at": datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
        "signals": df_signals,
    }
    with open(path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)

def load_last_signals(path: str = DEFAULT_LAST_SIGNALS_FILE):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)

def format_terminal_table(df_string: str, df: pd.DataFrame, num_rows: int = 3) -> str:
    lines = df_string.split('\n')
    if not lines: return df_string
    has_ltp = 'LTP' in lines[0] and 'Trigger' in df.columns
    if has_ltp:
        header = lines[0]
        col_start = header.find('LTP')
        col_end = header.find('Score', col_start) if 'Score' in header else len(header)
    for i in range(1, len(lines)):
        is_top = i <= num_rows
        row_color = "\033[92m" if is_top else ""
        reset_color = "\033[0m"
        line = lines[i]
        if has_ltp:
            row_idx = i - 1
            if row_idx < len(df):
                row = df.iloc[row_idx]
                if row.get('LTP', 0) > row.get('Trigger', 0):
                    ltp_sub = line[col_start:col_end]
                    stripped = ltp_sub.strip()
                    if stripped:
                        revert_color = row_color if is_top else reset_color
                        colored_sub = ltp_sub.replace(stripped, f"\033[96m{stripped}{revert_color}")
                        line = line[:col_start] + colored_sub + line[col_end:]
        lines[i] = f"{row_color}{line}{reset_color}" if is_top else line
    return '\n'.join(lines)

def export_to_pdf(df_signals, news_summary, filename):
    # Attempt to install fpdf2 if it's missing. It's imported as 'fpdf'.
    fpdf_module = install_and_import('fpdf2', 'fpdf', critical=False)
    if not fpdf_module:
        logger.warning("⚠️ PDF generation skipped because 'fpdf2' could not be installed.")
        return

    from fpdf.enums import XPos, YPos

    pdf = fpdf_module.FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(200, 10, text="Institutional Stock Scanner Report", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
    pdf.set_font("Helvetica", '', 10)
    pdf.cell(200, 10, text=f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
    pdf.ln(10)
    pdf.set_font("Helvetica", 'B', 12)
    pdf.cell(200, 10, text="TOP CONFIRMED SETUPS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Courier", '', 8) 
    display_cols = ['Symbol', 'Sector', 'LTP', 'Score', 'Trigger', 'Stop', 'Target', 'Trend', 'Vol_Ratio', 'RS_Pctl', 'RSI']
    cols_to_show = [c for c in display_cols if c in df_signals.columns]
    df_str = df_signals.head(10)[cols_to_show].to_string(index=False)
    for line in df_str.split('\n'):
        pdf.cell(200, 5, text=line.encode('latin-1', 'ignore').decode('latin-1'), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(5)
    pdf.set_font("Helvetica", 'B', 12)
    pdf.cell(200, 10, text="AI TRADE RATIONALE (TECHNICALS):", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", '', 10)
    for _, row in df_signals.head(10).iterrows():
        sym = row['Symbol']
        summary = row.get('AI_Summary', '')
        strength = row.get('Strength', '').replace('🚀', '').replace('🔥', '').replace('⚡', '').replace('⚠️', '').strip()
        text = f"[{sym}] ({strength}): {summary}"
        pdf.multi_cell(0, 5, text=text.encode('latin-1', 'ignore').decode('latin-1'))
        pdf.ln(2)
    pdf.ln(5)
    pdf.set_font("Helvetica", 'B', 12)
    pdf.cell(200, 10, text="GEMINI LIVE NEWS ANALYSIS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", '', 10)
    safe_news = news_summary.encode('latin-1', 'ignore').decode('latin-1')
    for line in safe_news.split('\n'):
        pdf.multi_cell(0, 5, text=line)
    try:
        pdf.output(filename)
        logger.info(f"📄 PDF Report successfully saved to {filename}")
    except Exception as e:
        logger.error(f"Failed to save PDF: {e}")

def fetch_dynamic_universe(fallback: list) -> list:
    logger.info(f"✅ Restricting scan universe to explicit list of {len(fallback)} symbols.")
    return fallback

def fetch_gemini_news(symbols: list) -> str:
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return "⚠️ GEMINI_API_KEY not found in .env file."
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash', tools=['google_search'])
        prompt = (
            "You are an expert Indian Stock Market analyst. "
            f"Find the most recent news headlines or catalysts for these NSE stocks: {', '.join(symbols)}. "
            "Provide exactly one short, punchy sentence per stock highlighting the most relevant recent news. "
            "Format as a clean bulleted list."
        )
        # Retry with exponential backoff for 503 errors
        for attempt in range(4):  # 1 initial call + 3 retries
            if shutdown_manager.is_shutdown():
                logger.warning("Shutdown initiated, cancelling Gemini news fetch.")
                return "⚠️ News fetch cancelled due to application shutdown."
            try:
                response = model.generate_content(prompt)
                break  # Success
            except Exception as e:
                if "503" in str(e) and attempt < 3:
                    base_delay = 5  # seconds
                    wait_time = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    logger.warning(f"Gemini API returned 503, retrying in {wait_time:.2f}s... (Attempt {attempt + 1}/4)")
                    time.sleep(wait_time)
                else:
                    raise  # Re-raise the last exception or if it's not a 503 error

        if response.prompt_feedback and response.prompt_feedback.block_reason:
            reason = response.prompt_feedback.block_reason.name
            logger.warning(f"Gemini prompt was blocked due to: {reason}")
            return f"⚠️ Gemini prompt was blocked: {reason}"

        return response.text.strip() if response and response.text else "⚠️ Gemini returned an empty response."
    except Exception as e:
        logger.error(f"Failed to fetch news from Gemini API: {e}", exc_info=True)
        return "⚠️ Failed to fetch news from Gemini API. See logs for details."

def print_btst_ranking_shift(df_signals: pd.DataFrame):
    if df_signals.empty or 'BTST_Final_Score' not in df_signals.columns:
        return
    before = df_signals.sort_values(by='Decision_Score', ascending=False).head(5)
    after = df_signals.sort_values(by='BTST_Final_Score', ascending=False).head(5)
    print("\nBTST RANKING BEFORE EXECUTION LAYER")
    before_cols = ['Symbol', 'Decision_Score', 'Execution_Score', 'Execution_Grade']
    print(before[[c for c in before_cols if c in before.columns]].to_string(index=False))
    print("\nBTST RANKING AFTER EXECUTION LAYER")
    after_cols = ['Symbol', 'BTST_Final_Score', 'Decision_Score', 'Execution_Score', 'Execution_Recommendation']
    print(after[[c for c in after_cols if c in after.columns]].to_string(index=False))

def print_top3_engine(df_signals: pd.DataFrame):
    ranked = add_decision_scores(df_signals)
    if ranked.empty: return
    for horizon in ['BTST', 'SWING']:
        subset = ranked[ranked.get('Horizon', '').astype(str).str.upper() == horizon].copy()
        if subset.empty: continue
        subset = subset.sort_values(by='Decision_Score', ascending=False).head(3)
        print(f"\nTOP {horizon}")
        for idx, (_, row) in enumerate(subset.iterrows(), start=1):
            print(f"{idx}. {row['Symbol']} | Confidence {row['Decision_Score']:.1f}/100 | Risk {row['Risk_Level']} | {row['Rank_Reason']}")

class SignalDB:
    def __init__(self):
        self.conn = sqlite3.connect('signals.db', check_same_thread=False)
        self._init_db()
        shutdown_manager.register(self.close)

    def __del__(self):
        self.close()

    def _init_db(self):
        cursor = self.conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME, symbol TEXT, strategy TEXT,
            score REAL, rs_pctl REAL, sector_rs REAL, adx REAL, vol_ratio REAL, entry REAL, stop REAL, target REAL
        )
        """)
        self.conn.commit()

    def log_signal(self, s):
        try:
            cursor = self.conn.cursor()
            strategy = str(s.get('Horizon', 'SWING')).upper()
            # For historical generation, the timestamp is passed in. Otherwise, use now.
            timestamp = s.get('timestamp', datetime.now(MARKET_TZ).isoformat(timespec='seconds'))

            cursor.execute("""
            INSERT INTO signals (timestamp, symbol, strategy, score, rs_pctl, sector_rs, adx, vol_ratio, entry, stop, target)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, s['Symbol'], strategy, s['Score'],
                s['RS_Pctl'], s['Sector_RS'], s['ADX'], s['Vol_Ratio'], s['Trigger'], s['Stop'], s['Target']))
            self.conn.commit()
        except (sqlite3.ProgrammingError, sqlite3.OperationalError) as e:
            if "closed" in str(e).lower():
                logger.warning("Database already closed, could not log signal.")
            else:
                raise


    def close(self):
        if self.conn:
            print("Closing database...")
            logger.info("Closing database connection.")
            self.conn.close()
            self.conn = None

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

def execute_macro_discovery(broker, scanner, strategy: str = 'SWING', force_refresh: bool = False, cache: DiscoveryCache = None, caller: str = "Unknown", point_in_time: Optional[datetime] = None) -> tuple:
    cache = cache or DiscoveryCache()
    if not force_refresh and cache.is_cache_valid(strategy):
        payload = cache.load_cache(strategy)
        discovered_df = payload["discovered_df"]
        watchlist = discovered_df.head(config.Discovery.TOP_N_WATCHLIST)["Symbol"].tolist()
        logger.info(f"⚡ Phase 1 cache hit: loaded {len(discovered_df)} ranked symbols from {cache.path}.")
        return watchlist, discovered_df

    logger.info(f"⚡ Phase 1: Initiating Broad Market Discovery Scan for {strategy}...")
    discovered_candidates = []
    if not point_in_time: broker._ensure_session()
    nifty_df = broker.fetch_ohlcv('Nifty 50', 'ONE_DAY', 400, force_refresh=force_refresh, caller=caller, end_date=point_in_time)
    regime = scanner.compute_market_regime(nifty_df)
    
    if strategy in ['BTST', 'GAP']:
        lookback_window = config.Discovery.LOOKBACK_BTST
    elif strategy == 'SILENT':
        lookback_window = config.Discovery.LOOKBACK_SILENT
    else:
        lookback_window = config.Discovery.LOOKBACK_SWING
    current_universe = fetch_dynamic_universe(config.Universe.TARGET_UNIVERSE)
    available_universe = broker.filter_available_symbols(current_universe)
    if not available_universe: return [], pd.DataFrame()
        
    raw_returns = get_universe_returns(broker, available_universe, lookback_days=lookback_window, force_refresh=force_refresh, caller=caller, end_date=point_in_time)
    if not raw_returns: return [], pd.DataFrame()
    ret_series = pd.Series({stock: data['return'] for stock, data in raw_returns.items()})
    
    sector_groups = {}
    for stock, stock_data in raw_returns.items():
        ret = stock_data.get('return', 0)
        sector = scanner.SECTOR_MAP.get(stock, 'OTHER')
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
            sector = sector_map.get(stock, 'OTHER')

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

            # Circuit Breaker: High Volume Reversal
            is_volume_shock = (strategy in ['BTST', 'GAP']) and (daily_vol_ratio > config.Discovery.VOL_SHOCK_RATIO_DAILY) and (rsi > config.Discovery.RSI_THRESHOLD - 10)

            is_silent = strategy == 'SILENT'
            silent_score = metrics.get('Silent_Score')

            if is_silent:
                # Stealth accumulation is judged on daily structure, not on the
                # momentum floors: a stock drifting up 1% a day sits mid-pack on
                # RS and rarely prints an overbought RSI.
                if silent_score is None or pd.isna(silent_score) or silent_score < config.Silent.SCORE_WATCH:
                    return None
                if cp < metrics.get('EMA50', cp) or rsi < config.Silent.RSI_THRESHOLD or rs_pctl < config.Silent.RS_PCT_THRESHOLD:
                    return None
            # Enforce Hard Trend Filters ONLY if a Volume Shock is NOT present
            elif not is_volume_shock:
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

            if is_silent:
                # Rank purely on the smoothness of the trend, lightly tilted by
                # relative strength so leaders surface first among equals.
                rank_score = min(100, (float(silent_score) * 0.85) + (min(rs_pctl, 100) * 0.15))

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
                'Silent_Score': float(silent_score) if silent_score is not None and not pd.isna(silent_score) else float('nan'),
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
        futures = {executor.submit(process_stock, stock): stock for stock in current_universe}
        for future in as_completed(futures):
            if shutdown_manager.is_shutdown():
                for f in futures: f.cancel()
                break
            res = future.result()
            if res: discovered_candidates.append(res)
            
    if shutdown_manager.is_shutdown(): return [], pd.DataFrame()
    discovered_df = pd.DataFrame(discovered_candidates)
    if discovered_df.empty: return [], pd.DataFrame()

    discovered_df = discovered_df.sort_values(by='Rank_Score', ascending=False)
    top_execution_watchlist = discovered_df.head(config.Discovery.TOP_N_WATCHLIST)['Symbol'].tolist()
    
    print("\n" + "="*80)
    print(f"🏆 TOP 10 SCANNED STOCKS RANKING (DISCOVERY PHASE)")
    df_slice = discovered_df.head(10)
    ranking_cols = ['Symbol', 'Sector', 'RS_Pctl', 'ADX', 'Rank_Score']
    if strategy == 'SILENT' and 'Silent_Score' in df_slice.columns:
        ranking_cols.insert(4, 'Silent_Score')
    df_str = df_slice[ranking_cols].to_string(index=False)
    print(format_terminal_table(df_str, df_slice))
    print("="*80 + "\n")
    
    from market_regime import MarketRegime
    regime_analyzer = MarketRegime(broker, scanner, current_universe, discovered_df)
    regime_analyzer.calculate_regime()
    regime_analyzer.print_report()

    cache.save_cache(discovered_df, top_execution_watchlist, strategy, regime)
    return top_execution_watchlist, discovered_df

def _scan_for_confirmation_signals(broker, scanner, watchlist, discovered_df, regime_mult, strategy, force_refresh, caller, point_in_time):
    """Scans a watchlist of stocks in parallel to find trading signals."""
    signals_triggered = []

    def scan_stock(stock):
        try:
            if shutdown_manager.is_shutdown(): return None
            is_silent = strategy.upper() == 'SILENT'
            # SILENT reads daily structure only, so the intraday fetches are skipped.
            df_15min = pd.DataFrame() if is_silent else broker.fetch_ohlcv(stock, 'FIFTEEN_MINUTE', 5, force_refresh=force_refresh, caller=caller, end_date=point_in_time)
            df_5min = broker.fetch_ohlcv(stock, 'FIVE_MINUTE', 5, force_refresh=force_refresh, caller=caller, end_date=point_in_time) if strategy.upper() in ['BTST', 'GAP'] else pd.DataFrame()
            if (is_silent or not df_15min.empty) and stock in discovered_df['Symbol'].values:
                s_row = discovered_df[discovered_df['Symbol'] == stock].iloc[0]
                df_daily = s_row.get('_Daily_DF', pd.DataFrame())
                daily_metrics = s_row.get('_Daily_Metrics')
                if df_daily.empty:
                    return None
                return scanner.scan(
                    stock, df_daily, df_15min,
                    rs_percentile=s_row['RS_Pctl'],
                    sector_rs=s_row['Sector_RS'],
                    regime_mult=regime_mult,
                    strategy=strategy,
                    daily_metrics=daily_metrics,
                    df_5min=df_5min,
                )
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

def generate_oneline_reason(row: pd.Series) -> str:
    """Generates a concise, one-line reason for the recommendation."""
    recommendation = row.get('Execution_Recommendation')

    # --- BUY TODAY ---
    if recommendation == 'BUY TODAY':
        reasons = []
        if row.get('Breakout250') == 'YES':
            reasons.append("Breakout")
        if row.get('Closing_Strength') == 'Strong':
            reasons.append("Strong Close")
        if row.get('Above_VWAP') == 'YES':
            reasons.append("Above VWAP")
        if row.get('Intraday_Trend') == 'Strong':
            reasons.append("Strong Momentum")
        if not reasons:
            return "Strong execution signals"
        return " + ".join(reasons[:3])

    # --- WATCH ---
    if recommendation == 'WATCH':
        if row.get('Closing_Strength') == 'Weak':
            return "Weak closing strength"
        if row.get('Intraday_Trend') != 'Strong':
            return "Lacks strong intraday momentum"
        if row.get('Execution_Grade') == 'B':
            return "Execution improving, needs confirmation"
        return "Awaiting stronger confirmation"

    # --- WAIT / REJECTED ---
    if recommendation in ['WAIT', 'NO BUY']:
        # Find the first reason for rejection
        if row.get('Trend') == 'BEAR':
            return "Daily trend is bearish"
        if row.get('Above_VWAP') == 'NO':
            return "Price below VWAP"
        if row.get('Execution_Grade') in ['C', 'D']:
            return f"Weak execution (Grade {row.get('Execution_Grade')})"
        if not row.get('_Afternoon_Momentum', False):
            return "Weak afternoon momentum"
        if row.get('Closing_Strength') == 'Weak':
            return "Weak closing strength"
        return "Execution criteria not met"

    return "N/A"

def _display_clean_report(df_signals: pd.DataFrame, strategy: str, discovered_df: pd.DataFrame):
    """Displays a concise, actionable report for traders."""
    os.system('cls' if os.name == 'nt' else 'clear')

    df_signals['Reason'] = df_signals.apply(generate_oneline_reason, axis=1)

    score_col = 'BTST_Final_Score' if 'BTST_Final_Score' in df_signals.columns else 'Decision_Score'
    df_signals = df_signals.sort_values(by=score_col, ascending=False)

    market_regime = df_signals['Market_Regime'].iloc[0] if not df_signals.empty and 'Market_Regime' in df_signals.columns else 'N/A'
    qualified_count = len(discovered_df) if discovered_df is not None else len(df_signals)

    buy_today_df = df_signals[df_signals['Execution_Recommendation'] == 'BUY TODAY']
    watch_df = df_signals[df_signals['Execution_Recommendation'] == 'WATCH']
    wait_df = df_signals[df_signals['Execution_Recommendation'].isin(['WAIT', 'NO BUY'])]

    # --- Main Header ---
    print("\n" + "#" * 50)
    print(f"🚀 {strategy.upper()} REPORT")
    print(f"Date:         {datetime.now(MARKET_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Market Bias:  {market_regime}")
    print(f"Qualified:    {qualified_count} stocks")
    print("#" * 50)

    # --- BUY TODAY Section ---
    print("\n🟢 BUY TODAY")
    if not buy_today_df.empty:
        buy_display = buy_today_df.head(5).copy()
        buy_display['Rank'] = range(1, len(buy_display) + 1)
        buy_display['Final_Score'] = buy_display[score_col]
        
        display_cols = ['Rank', 'Symbol', 'Final_Score', 'Trigger', 'Stop', 'Target', 'Risk_Level', 'Reason']
        buy_display_filtered = buy_display[[c for c in display_cols if c in buy_display.columns]]
        
        buy_display_renamed = buy_display_filtered.rename(columns={
            'Final_Score': 'Score',
            'Trigger': 'Entry',
            'Risk_Level': 'Risk'
        })
        print(buy_display_renamed.to_string(index=False))
    else:
        print("No high-conviction buy signals found.")

    # --- WATCH Section ---
    print("\n" + "#" * 50)
    print("🟡 WATCH")
    if not watch_df.empty:
        watch_display = watch_df.head(5).copy()
        for _, row in watch_display.iterrows():
            print(f"{row['Symbol']:<15} {row['Reason']}")
    else:
        print("No stocks to watch.")

    # --- WAIT / REJECTED Section ---
    print("\n" + "#" * 50)
    print("🔴 WAIT / REJECTED")
    if not wait_df.empty:
        wait_display = wait_df.head(10).copy()
        for _, row in wait_display.iterrows():
            print(f"{row['Symbol']:<15} {row['Reason']}")
    else:
        print("No rejected signals in this run.")

    # --- MARKET SUMMARY Section ---
    print("\n" + "#" * 50)
    print("📊 MARKET SUMMARY")
    print(f"  Stocks Scanned (Phase 1):    {qualified_count}")
    print(f"  Qualified (Phase 2):         {len(df_signals)}")
    print(f"  BUY TODAY:                   {len(buy_today_df)}")
    print(f"  WATCH:                       {len(watch_df)}")
    print(f"  WAIT / REJECTED:             {len(wait_df)}")
    
    avg_score = buy_today_df[score_col].mean() if not buy_today_df.empty else float('nan')
    print(f"  Average Final Score (BUY):   {avg_score:.2f}" if not pd.isna(avg_score) else "N/A")
    print(f"  Market Trend:                {market_regime}")
    print("#" * 50 + "\n")

def _display_debug_report(df_signals, strategy):
    """Clears the console and prints detailed debug tables for the top scan results."""
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n" + "="*100)
    print(f"🚨 TOP 10 CONFIRMED SETUPS (DEBUG MODE) 🚨")
    if strategy.upper() in ['BTST', 'GAP']:
        print_btst_ranking_shift(df_signals)
    
    print("\n--- FULL DATAFRAME (TOP 10) ---")
    display_cols = [
        'Symbol', 'Sector', 'LTP', 'BTST_Final_Score', 'Decision_Score', 'Execution_Score',
        'Execution_Grade', 'VWAP', 'Above_VWAP', 'Intraday_Trend', 'Closing_Strength',
        'Execution_Recommendation', 'Score', 'Strength', 'Trigger', 'Stop', 'Target',
        'Risk_Reward', 'Risk_Level', 'Trend', 'Liquidity', 'Breakout250', 'Vol_Ratio',
        'RS_Pctl', 'RSI'
    ]
    cols_to_show = [c for c in display_cols if c in df_signals.columns]
    df_slice = df_signals.head(10)
    df_str = df_slice[cols_to_show].to_string(index=False)
    print(format_terminal_table(df_str, df_slice))
    print("="*100 + "\n")

    print("\n--- TOP 3 ENGINE (DECISION SCORE) ---")
    print_top3_engine(df_signals)
    print("="*100 + "\n")

    print("\n--- AI RATIONALE (TECHNICALS) ---")
    for _, row in df_signals.head(10).iterrows():
        print(f" {row.get('Strength', '')} | [{row['Symbol']}] : {row.get('AI_Summary', '')}")
    print("="*100 + "\n")

def _display_confirmation_results(df_signals, strategy, discovered_df):
    """Dispatches to the correct display function based on debug configuration."""
    if config.AppConfig.DEBUG_REPORT:
        _display_debug_report(df_signals, strategy)
    else:
        _display_clean_report(df_signals, strategy, discovered_df)

def _persist_confirmation_signals(signals_triggered):
    """Logs a list of signal dictionaries to the database."""
    db = SignalDB()
    try:
        if shutdown_manager.is_shutdown(): return
        for sig in signals_triggered:
            db.log_signal(sig)
    finally:
        db.close()

def run_manual_scan(broker, scanner, watchlist, discovered_df, strategy: str = 'SWING', persist: bool = True, display: bool = True, force_refresh: bool = False, caller: str = "Unknown", point_in_time: Optional[datetime] = None) -> pd.DataFrame:
    logger.info(f"🔥 Phase 2: Running Micro-Volume Confirmation Scan for Best Stocks under {strategy}...")
    if '_Regime_Mult' in discovered_df.columns and not discovered_df.empty:
        regime_mult = discovered_df['_Regime_Mult'].iloc[0]
    else:
        nifty_df = broker.fetch_ohlcv('Nifty 50', 'ONE_DAY', 400, force_refresh=force_refresh, caller=caller, end_date=point_in_time)
        regime_mult = scanner.compute_market_regime(nifty_df)['multiplier']
    
    signals_triggered = _scan_for_confirmation_signals(
        broker, scanner, watchlist, discovered_df, regime_mult, strategy, force_refresh, caller, point_in_time
    )

    if not signals_triggered:
        logger.info("No actionable signals found during this scan.")
        return pd.DataFrame()

    df_signals = add_decision_scores(pd.DataFrame(signals_triggered))
    if strategy.upper() == 'SILENT':
        # add_decision_scores deliberately rewards *less* crowded momentum
        # setups by inverting Score, which would rank the quietest accumulation
        # last. Silent signals are ranked on their own footprint score instead.
        df_signals['Decision_Score'] = df_signals['Score']
        df_signals = df_signals.sort_values(by='Score', ascending=False)
    elif strategy.upper() in ['BTST', 'GAP'] and 'BTST_Final_Score' in df_signals.columns:
        df_signals = df_signals.sort_values(by='BTST_Final_Score', ascending=False)
    else:
        df_signals = add_decision_scores(pd.DataFrame(signals_triggered))
        df_signals = df_signals.sort_values(by='Decision_Score', ascending=False)

    if display:
        _display_confirmation_results(df_signals, strategy, discovered_df)

    if persist:
        _persist_confirmation_signals(signals_triggered)
        
    return df_signals

def load_cached_discovery(strategy: str = 'SWING', cache: DiscoveryCache = None, top_n: int = 20):
    """Phase 2 cache reader: returns only the best ranked names for intraday confirmation."""
    cache = cache or DiscoveryCache()
    if not cache.is_cache_valid(strategy):
        return [], pd.DataFrame()
    payload = cache.load_cache(strategy)
    discovered_df = payload.get("discovered_df", pd.DataFrame()).sort_values(by="Rank_Score", ascending=False)
    return discovered_df.head(top_n)["Symbol"].tolist(), discovered_df

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

def run_report_pipeline(df_signals: pd.DataFrame, strategy: str = 'BTST'):
    """Phase 3 reporting is intentionally isolated so reports never delay signal display."""
    if df_signals is None or df_signals.empty or shutdown_manager.is_shutdown():
        logger.info("No signals available for report pipeline or shutdown initiated.")
        return
    save_last_signals(df_signals)
    top_df = df_signals.head(10)
    top_symbols = top_df['Symbol'].tolist()
    logger.info("📰 Background report: fetching Gemini news...")
    news_summary = fetch_gemini_news(top_symbols)

    if shutdown_manager.is_shutdown(): return

    print("\n📰 GEMINI LIVE NEWS ANALYSIS:")
    print(news_summary)
    print("="*100 + "\n")

    db = SignalDB()
    try:
        for _, sig in df_signals.iterrows():
            if shutdown_manager.is_shutdown(): break
            db.log_signal(sig)
    finally:
        db.close() # SignalDB close is now idempotent

    if shutdown_manager.is_shutdown(): return

    pdf_file = f"scan_report_{datetime.now().strftime('%Y_%m_%d_%H%M')}.pdf"
    export_to_pdf(top_df, news_summary, pdf_file)
    print(f"📄 Background {strategy.upper()} report saved to {pdf_file}")

def start_background_report(df_signals: pd.DataFrame, strategy: str = 'BTST'):
    """Starts Phase 3 asynchronously after actionable signals are already on screen."""
    worker = threading.Thread(target=run_report_pipeline, args=(df_signals.copy(), strategy), name="ReportGeneratorThread", daemon=True)
    worker.start()
    _background_workers.append(worker)
    return worker

def run_on_demand_scan():
    """
    Main entry point for a single, on-demand scan.
    Orchestrates the discovery and confirmation phases and handles top-level errors.
    """
    try:
        logger.info("🚀 Running On-Demand Scanner...")
        broker = DataBroker()
        shutdown_manager.register(broker.close_session)
        scanner = HybridScanner(base_multiplier=2.0, alpha=0.5, max_risk_per_trade=5000, max_capital_per_trade=100000)

        # Phase 1: Broad market discovery
        execution_watchlist, discovered_df = execute_macro_discovery(broker, scanner, strategy='SWING', caller="OnDemandScan")

        # Phase 2: Confirmation scan on top candidates
        if execution_watchlist and not shutdown_manager.is_shutdown():
            run_manual_scan(broker, scanner, execution_watchlist, discovered_df, strategy='SWING', caller="OnDemandScan")
        else:
            logger.warning("Discovery phase yielded no candidates or shutdown initiated. On-demand scan complete with no signals found.")
        
        if not shutdown_manager.is_shutdown():
            logger.info("✅ On-Demand Scan finished successfully.")

    except Exception as e:
        if not shutdown_manager.is_shutdown():
            logger.error("❌ An unexpected error occurred during the on-demand scan.", exc_info=True)

if __name__ == "__main__":
    try:
        run_on_demand_scan()
        # Wait for shutdown signal if there are background tasks, otherwise exit
        if not _background_workers:
             shutdown_manager.initiate_shutdown()
        else:
             shutdown_manager.shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received in main. Shutting down.")
    finally:
        if not shutdown_manager.is_shutdown():
            shutdown_manager.initiate_shutdown()

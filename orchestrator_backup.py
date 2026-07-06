import os
import time
import logging
import sqlite3
import random
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import config
from data_broker import DataBroker
from scanner_engine import HybridScanner
from utils import add_decision_scores, install_and_import

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("Asia/Kolkata")
genai = install_and_import('google-generativeai', 'google.generativeai')

def format_terminal_table(df_string: str, df: pd.DataFrame, num_rows: int = 3) -> str:
    """Colors top rows green and dynamically highlights the LTP in cyan if > Trigger."""
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
    """Generates a clean PDF report containing technicals and news."""
    # Attempt to install fpdf2 if it's missing. It's imported as 'fpdf'.
    fpdf_module = install_and_import('fpdf2', 'fpdf', critical=False)
    if not fpdf_module:
        logger.warning("⚠️ PDF generation skipped because 'fpdf2' could not be installed.")
        return

    from fpdf.enums import XPos, YPos

    pdf = fpdf_module.FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    
    # 1. Header Section
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(200, 10, text="Institutional Stock Scanner Report", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
    pdf.set_font("Helvetica", '', 10)
    pdf.cell(200, 10, text=f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
    pdf.ln(10)
    
    # 2. Technicals Data Table (Using Monospace font for alignment)
    pdf.set_font("Helvetica", 'B', 12)
    pdf.cell(200, 10, text="TOP CONFIRMED SETUPS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Courier", '', 8) 
    
    display_cols = ['Symbol', 'Sector', 'LTP', 'Score', 'Trigger', 'Stop', 'Target', 'Trend', 'Vol_Ratio', 'RS_Pctl', 'RSI']
    cols_to_show = [c for c in display_cols if c in df_signals.columns]
    df_str = df_signals.head(10)[cols_to_show].to_string(index=False)
    
    for line in df_str.split('\n'):
        pdf.cell(200, 5, text=line.encode('latin-1', 'ignore').decode('latin-1'), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(5)
    
    # 3. AI Rationale
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
    
    # 4. Gemini News
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
    """Bypassed dynamic fetch to use only the explicit target universe."""
    logger.info(f"✅ Restricting scan universe to explicit list of {len(fallback)} symbols.")
    return fallback

def fetch_gemini_news(symbols: list) -> str:
    """Queries the real Gemini API for live news summaries using Search Grounding."""
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return "⚠️ GEMINI_API_KEY not found in .env file."
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            'gemini-1.5-flash', tools=['google_search'])
        prompt = (
            "You are an expert Indian Stock Market analyst. "
            f"Find the most recent news headlines or catalysts for these NSE stocks: {', '.join(symbols)}. "
            "Provide exactly one short, punchy sentence per stock highlighting the most relevant recent news. "
            "At the end of each sentence, append the exact date and time of their next upcoming earnings report (e.g., ' | Next Earnings: Oct 15, 4:00 PM'). If unknown, state ' | Earnings: TBA'. "
            "Format as a clean bulleted list."
        )
        
        # Retry with exponential backoff for 503 errors
        for attempt in range(4):  # 1 initial call + 3 retries
            try:
                response = model.generate_content(prompt)
                if response.prompt_feedback and response.prompt_feedback.block_reason:
                    reason = response.prompt_feedback.block_reason.name
                    logger.warning(f"Gemini prompt was blocked due to: {reason}")
                    return f"⚠️ Gemini prompt was blocked: {reason}"
                return response.text.strip() if response and response.text else "⚠️ Gemini returned an empty response."
            except Exception as e:
                if "503" in str(e) and attempt < 3:
                    base_delay = 5  # seconds
                    wait_time = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    logger.warning(f"Gemini API returned 503, retrying in {wait_time:.2f}s... (Attempt {attempt + 1}/4)")
                    time.sleep(wait_time)
                else:
                    raise  # Re-raise the last exception or if it's not a 503 error
    except Exception as e:
        logger.error(f"Failed to fetch news from Gemini API: {e}", exc_info=True)
        return "⚠️ Failed to fetch news from Gemini API. See logs for details."

def print_top3_engine(df_signals: pd.DataFrame):
    ranked = add_decision_scores(df_signals)
    if ranked.empty:
        return

    for horizon in ['BTST', 'SWING']:
        subset = ranked[ranked.get('Horizon', '').astype(str).str.upper() == horizon].copy()
        if subset.empty:
            print(f"\nTOP {horizon}\nNo qualified {horizon} setups in this scan.")
            continue

        subset = subset.sort_values(by='Decision_Score', ascending=False).head(3)
        print(f"\nTOP {horizon}")
        for idx, (_, row) in enumerate(subset.iterrows(), start=1):
            print(
                f"{idx}. {row['Symbol']} | Confidence {row['Decision_Score']:.1f}/100 | "
                f"Risk {row['Risk_Level']} | {row['Rank_Reason']}"
            )

class SignalDB:
    def __init__(self):
        self.conn = sqlite3.connect('signals.db')
        self._init_db()

    def _init_db(self):
        cursor = self.conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            symbol TEXT,
            strategy TEXT,
            score REAL,
            rs_pctl REAL,
            sector_rs REAL,
            adx REAL,
            vol_ratio REAL,
            entry REAL,
            stop REAL,
            target REAL
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            signal_id INTEGER PRIMARY KEY,
            outcome TEXT,
            return REAL
        )
        """)

        self.conn.commit()

        # Auto-migrate old database
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(signals)")}
        if 'strategy' not in columns:
            cursor.execute(
                "ALTER TABLE signals ADD COLUMN strategy TEXT"
            )
            self.conn.commit()
            logger.info("✅ Added strategy column to existing database.")

    def log_signal(self, s):
        cursor = self.conn.cursor()

        strategy = str(s.get('Horizon', 'SWING')).upper()

        cursor.execute("""
        INSERT INTO signals (
            timestamp,
            symbol,
            strategy,
            score,
            rs_pctl,
            sector_rs,
            adx,
            vol_ratio,
            entry,
            stop,
            target
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(MARKET_TZ).isoformat(timespec='seconds'),
            s['Symbol'],
            strategy,
            s['Score'],
            s['RS_Pctl'],
            s['Sector_RS'],
            s['ADX'],
            s['Vol_Ratio'],
            s['Trigger'],
            s['Stop'],
            s['Target']
        ))

        self.conn.commit()

    def close(self):
        self.conn.close()

def get_universe_returns(broker, universe, lookback_days: int = 90):
    """Fetches historical returns over a dynamic lookback window."""
    returns = {}
    total = len(universe)
    
    def fetch_return(stock):
        df = broker.fetch_ohlcv(stock, 'ONE_DAY', 400)
        if not df.empty and len(df) >= lookback_days:
            return stock, (df['Close'].iloc[-1] / df['Close'].iloc[-lookback_days]) - 1
        return stock, None

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_return, stock): stock for stock in universe}
        for i, future in enumerate(as_completed(futures)):
            print(f"[{i+1}/{total}] Fetching {lookback_days}-day returns...", end='\r')
            stock, ret = future.result()
            if ret is not None:
                returns[stock] = ret
                
    print(" " * 60, end='\r') 
    return returns

def execute_macro_discovery(broker, scanner, strategy: str = 'SWING') -> tuple:
    """Scans target universe to filter alpha candidates using dynamic strategy horizons."""
    logger.info(f"⚡ Phase 1: Initiating Broad Market Discovery Scan for {strategy}...")
    discovered_candidates = []

    nifty_df = broker.fetch_ohlcv('Nifty 50', 'ONE_DAY', 400)
    regime = scanner.compute_market_regime(nifty_df)
    if regime['label'] == 'EXTREME_BEAR':
        logger.warning("Market in Extreme Bearish regime. Proceeding with caution for discovery.")
    elif regime['label'] == 'BEARISH_RECOVERY':
        logger.info("Market is below 200 EMA but showing short-term bullish recovery.")

    # 1. Dynamic Lookback Window Assignment
    lookback_window = 14 if strategy in ['BTST', 'GAP'] else 90
    
    current_universe = fetch_dynamic_universe(config.Universe.TARGET_UNIVERSE)
    available_universe = broker.filter_available_symbols(current_universe)
    if not available_universe:
        logger.warning("No universe symbols are available in the current scrip master. Update the token mapping file.")
        return [], pd.DataFrame()
        
    raw_returns = get_universe_returns(broker, available_universe, lookback_days=lookback_window)
    if not raw_returns:
        logger.warning("No universe returns could be calculated from available symbols. Check scrip master coverage and API access.")
        return [], pd.DataFrame()
    
    ret_series = pd.Series(raw_returns)
    
    # 2. Sector Average Returns
    sector_groups = {}
    for stock, ret in raw_returns.items():
        sector = scanner.SECTOR_MAP.get(stock, 'OTHER')
        sector_groups.setdefault(sector, []).append(ret)
    sector_averages = {k: sum(v)/len(v) for k, v in sector_groups.items()}

    def process_stock(stock):
        try:
            if stock not in raw_returns: return None
            
            df_daily = broker.fetch_ohlcv(stock, 'ONE_DAY', 400)
            if df_daily.empty: return None
            
            metrics = scanner.compute_daily_metrics(df_daily)
            if metrics is None: return None
            
            # Institutional RS Logic
            stock_ret = raw_returns[stock]
            rs_pctl = (ret_series < stock_ret).mean() * 100
            sec_avg = sector_averages.get(scanner.SECTOR_MAP.get(stock, 'OTHER'), 0)
            sector_rs = max(0, min(100, 50 + (stock_ret - sec_avg) * 100))
            
            cp = metrics['Close']
            rsi = metrics.get('RSI', 50)
            
            # 🛑 HARD FILTER: Drop Illiquid Stocks permanently
            if metrics.get('Avg_Traded_Value_20d', 0) < 100_000_000:
                return None

            # Calculate Daily Volume Ratio for V-Shaped Reversal Detection
            recent_vol = df_daily['Volume'].iloc[-3:].mean()
            historic_vol = df_daily['Volume'].iloc[-23:-3].mean()
            daily_vol_ratio = recent_vol / historic_vol if historic_vol > 0 else 0

            # Circuit Breaker: High Volume Reversal Override for BTST/Intraday Velocity
            is_volume_shock = (strategy in ['BTST', 'GAP']) and (daily_vol_ratio > 2.5) and (rsi > 40)

            # Enforce Hard Trend Filters ONLY if a Volume Shock is NOT present
            if not is_volume_shock:
                if cp < metrics['EMA50'] or rs_pctl < 40.0 or rsi < 50:
                    return None

            pivot = metrics['Pivot_50']
            pct_from_pivot = ((cp - pivot) / pivot) * 100 if pivot and pivot != 0 else 0.0
            
            # Reduce distance penalties dynamically when catching highly volatile breakout points
            distance_penalty_multiplier = 0.2 if is_volume_shock else 0.5
            distance_divisor = max(1.0, abs(pct_from_pivot) * distance_penalty_multiplier)
            
            # Institutional Ranking Formula (Applied to all candidates)
            rank_score = (
                (min(metrics['adx'], 50) * 0.4) +
                (min(rs_pctl, 100) * 0.8) +
                (min(sector_rs, 100) * 0.4) +
                (min(rsi, 80) * 0.2)
            ) / distance_divisor
            rank_score = min(rank_score, 100)

            return {
                'Symbol': stock,
                'Sector': scanner.SECTOR_MAP.get(stock, 'OTHER'),
                'RS_Pctl': rs_pctl,
                'Sector_RS': sector_rs,
                'ADX': metrics['adx'],
                'Distance': pct_from_pivot,
                'Rank_Score': rank_score,
                'EMA_Delta': ((cp - metrics['EMA20']) / metrics['EMA20']) * 100
            }
        except Exception as e:
            logger.debug(f"Bypassing {stock} discovery parameters: {e}")
        return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(process_stock, stock) for stock in current_universe]
        for future in as_completed(futures):
            res = future.result()
            if res:
                discovered_candidates.append(res)
            
    # Sort candidates by trend velocity (ADX) and immediate proximity to breakout boundaries
    discovered_df = pd.DataFrame(discovered_candidates)
    if discovered_df.empty:
        return [], pd.DataFrame()
        
    discovered_df = discovered_df.sort_values(by='Rank_Score', ascending=False)
    
    # Pass Top 10 strictly bullish candidates to the next phase
    top_execution_watchlist = discovered_df.head(10)['Symbol'].tolist()
    logger.info(f"✅ Discovery Phase Complete. Extracted top {len(top_execution_watchlist)} candidates for Execution.")
    logger.info(f"📋 Top 10 Watchlist: {', '.join(top_execution_watchlist)}")
    
    print("\n" + "="*80)
    print(f"🏆 TOP 10 SCANNED STOCKS RANKING (DISCOVERY PHASE)")
    df_slice = discovered_df.head(10)
    df_str = df_slice[['Symbol', 'Sector', 'RS_Pctl', 'ADX', 'Rank_Score']].to_string(index=False)
    print(format_terminal_table(df_str, df_slice))
    print("="*80 + "\n")
    
    return top_execution_watchlist, discovered_df

def _scan_watchlist_concurrently(broker, scanner, watchlist, discovered_df, regime_mult, strategy):
    """
    Scans a watchlist of stocks in parallel to find trading signals.
    """
    signals_triggered = []

    def scan_stock(stock):
        try:
            df_daily = broker.fetch_ohlcv(stock, 'ONE_DAY', 400)
            df_15min = broker.fetch_ohlcv(stock, 'FIFTEEN_MINUTE', 5)
            
            if not df_daily.empty and not df_15min.empty and stock in discovered_df['Symbol'].values:
                s_row = discovered_df[discovered_df['Symbol'] == stock].iloc[0]
                return scanner.scan(
                    stock, df_daily, df_15min, 
                    rs_percentile=s_row['RS_Pctl'], 
                    sector_rs=s_row['Sector_RS'], 
                    regime_mult=regime_mult, 
                    strategy=strategy
                )
        except Exception as e:
            logger.error(f"Micro-scan error for {stock}: {e}")
        return None

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(scan_stock, stock) for stock in watchlist]
        for future in as_completed(futures):
            signal = future.result()
            if signal:
                signals_triggered.append(signal)
    
    return signals_triggered

def _display_scan_results(df_signals: pd.DataFrame):
    """
    Clears the console and prints formatted tables for the top scan results.
    """
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n" + "="*100)
    print(f"🚨 TOP 10 CONFIRMED SETUPS 🚨")
    display_cols = ['Symbol', 'Sector', 'LTP', 'Decision_Score', 'Score', 'Strength', 'Trigger', 'Stop', 'Target', 'Risk_Reward', 'Risk_Level', 'Trend', 'Liquidity', 'Breakout250', 'Vol_Ratio', 'RS_Pctl', 'RSI']
    cols_to_show = [c for c in display_cols if c in df_signals.columns]
    df_slice = df_signals.head(10)
    df_str = df_slice[cols_to_show].to_string(index=False)
    print(format_terminal_table(df_str, df_slice))
    print("="*100 + "\n")
    
    print_top3_engine(df_signals)
    print("="*100 + "\n")

    print("🤖 AI TRADE RATIONALE (TECHNICALS):")
    for _, row in df_signals.head(10).iterrows():
        print(f" {row.get('Strength', '')} | [{row['Symbol']}] : {row.get('AI_Summary', '')}")
    print("="*100 + "\n")

def _report_and_persist_signals(df_signals: pd.DataFrame):
    """
    Persists signals to DB/CSV, fetches news, and generates a PDF report.
    """
    # Log to Database for Backtesting
    db = SignalDB()
    try:
        for _, row in df_signals.iterrows():
            sig = row.to_dict()
            db.log_signal(sig)
    finally:
        db.close()

    # Fetch and display news
    print("📰 GEMINI LIVE NEWS ANALYSIS:")
    top_symbols = df_signals.head(10)['Symbol'].tolist()
    print("Fetching live news from the web via Gemini API... (this may take 3-5 seconds)")
    news_summary = fetch_gemini_news(top_symbols)
    print(news_summary)
    print("="*100 + "\n")
    
    # Save to CSV and PDF
    log_file = f"manual_scan_results_{datetime.now().strftime('%Y_%m_%d_%H%M')}.csv"
    df_signals.to_csv(log_file, index=False)
    logger.info(f"📁 Full detailed report saved to {log_file}")
    
    pdf_file = f"scan_report_{datetime.now().strftime('%Y_%m_%d_%H%M')}.pdf"
    export_to_pdf(df_signals, news_summary, pdf_file)

def run_manual_scan(broker, scanner, watchlist, discovered_df, strategy: str = 'SWING'):
    """
    Orchestrates the confirmation scan over a discovery watchlist, displays results,
    and generates reports.
    """
    logger.info(f"🔥 Phase 2: Running Micro-Volume Confirmation Scan for Best Stocks under {strategy}...")

    nifty_df = broker.fetch_ohlcv('Nifty 50', 'ONE_DAY', 400)
    regime = scanner.compute_market_regime(nifty_df)
    
    signals_triggered = _scan_watchlist_concurrently(
        broker, scanner, watchlist, discovered_df, regime['multiplier'], strategy
    )
        
    if signals_triggered:
        df_signals = add_decision_scores(pd.DataFrame(signals_triggered))
        df_signals = df_signals.sort_values(by='Decision_Score', ascending=False)
        
        _display_scan_results(df_signals)
        _report_and_persist_signals(df_signals)
    else:
        logger.info("No actionable signals found during this scan.")

def run_on_demand_scan():
    broker = DataBroker()
    scanner = HybridScanner(base_multiplier=2.0, alpha=0.5, max_risk_per_trade=5000, max_capital_per_trade=100000)
    
    logger.info("🚀 Running On-Demand Scanner...")
    execution_watchlist, discovered_df = execute_macro_discovery(broker, scanner, strategy='SWING')
    if execution_watchlist:
        run_manual_scan(broker, scanner, execution_watchlist, discovered_df, strategy='SWING')
    else:
        logger.warning("Discovery phase yielded no candidates.")

if __name__ == "__main__":
    run_on_demand_scan()

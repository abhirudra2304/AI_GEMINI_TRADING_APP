import os
import time
import logging
import random
import threading
from datetime import datetime
from typing import Optional
import pandas as pd

import config
from utils import install_and_import, add_decision_scores
from lifecycle_manager import shutdown_manager
from database import SignalDB
from cache import save_last_signals

# genai and types will be imported on-demand inside fetch_gemini_news to handle import errors gracefully.
logger = logging.getLogger(__name__)


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

def export_to_pdf(df_signals, news_summary, filename, strategy: str = 'BTST'):
    # Attempt to install fpdf2 if it's missing. It's imported as 'fpdf'.
    fpdf_module = install_and_import('fpdf2', critical=False)
    if not fpdf_module:
        logger.warning("⚠️ PDF generation skipped because 'fpdf2' could not be installed.")
        return

    # Get enums from the dynamically imported module to avoid resolution errors.
    XPos = fpdf_module.enums.XPos
    YPos = fpdf_module.enums.YPos

    pdf = fpdf_module.FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(200, 10, text=f"Institutional Stock Scanner Report - {strategy.upper()} SCAN", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
    pdf.set_font("Helvetica", '', 10)
    pdf.cell(200, 10, text=f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
    pdf.ln(10)
    pdf.set_font("Helvetica", 'B', 12)
    pdf.cell(200, 10, text=f"TOP CONFIRMED {strategy.upper()} SETUPS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
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
    # FIX: Replace non-breaking spaces with regular spaces and strip Markdown asterisks
    safe_news = news_summary.replace('\xa0', ' ').replace('**', '').encode('latin-1', 'ignore').decode('latin-1')
    for line in safe_news.split('\n'):
        # FIX: Ignore long unbreakable dividers (==== or ----) that crash fpdf2
        stripped = line.strip()
        if len(stripped) > 20 and (set(stripped) == {'='} or set(stripped) == {'-'}):
            continue
            
        try:
            # FIX: Catch rendering errors so the thread doesn't crash
            pdf.multi_cell(0, 5, text=line)
        except Exception as e:
            logger.warning(f"⚠️ Skipped rendering a line in PDF due to fpdf2 error: {e}")

    try:
        pdf.output(filename)
        logger.info(f"📄 PDF Report successfully saved to {filename}")
    except Exception as e:
        logger.error(f"Failed to save PDF: {e}")

def export_eod_pdf(results: dict, filename: str, news_summary: Optional[str] = None) -> bool:
    """
    Saves the consolidated `eod` report (BTST/SWING/EMFB Top 5s) to a PDF.

    `news_summary` is optional and None by default - the primary/fast eod PDF
    (see main.py's run_eod) deliberately does NOT fetch Gemini news before
    calling this, since `eod` is meant to finish with real margin before market
    close and a live LLM call is an extra unpredictable-latency dependency
    right before that deadline. Passing a pre-fetched summary here (as
    run_eod's background news-enhancement thread does) adds a news section
    without this function itself ever being the one to make that slow call.
    Returns True on success so callers (e.g. an unattended scheduled run) know
    whether there's actually a file to point a notification at.
    """
    fpdf_module = install_and_import('fpdf2', critical=False)
    if not fpdf_module:
        logger.warning("⚠️ EOD PDF generation skipped because 'fpdf2' could not be installed.")
        return False

    XPos = fpdf_module.enums.XPos
    YPos = fpdf_module.enums.YPos

    pdf = fpdf_module.FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(200, 10, text="EOD Consolidated Report", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
    pdf.set_font("Helvetica", '', 10)
    pdf.cell(200, 10, text=f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
    pdf.ln(5)

    stale_run_warning = results.get('_stale_run_warning')
    if stale_run_warning:
        pdf.set_font("Helvetica", 'B', 11)
        pdf.set_text_color(200, 0, 0)
        pdf.multi_cell(0, 5, text=stale_run_warning.encode('latin-1', 'ignore').decode('latin-1'))
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    sections = [
        ("BTST PICKS", results.get('BTST', pd.DataFrame()), 'BTST_Final_Score',
         ['Symbol', 'Sector', 'BTST_Final_Score', 'Strength', 'Trigger', 'Stop', 'Target', 'Risk_Reward', 'Data_Stale']),
        ("SWING PICKS", results.get('SWING', pd.DataFrame()), 'Decision_Score',
         ['Symbol', 'Sector', 'Decision_Score', 'Strength', 'Trigger', 'Stop', 'Target', 'Risk_Reward', 'Data_Stale']),
        ("EMERGING MOMENTUM (EMFB) PICKS", results.get('EMFB', pd.DataFrame()), 'EMFB_Score',
         ['Symbol', 'Sector', 'EMFB_Score', 'Confidence', 'Trigger', 'Stop', 'Target', 'Data_Stale']),
    ]

    for title, df, score_col, cols in sections:
        pdf.set_font("Helvetica", 'B', 12)
        pdf.cell(200, 10, text=title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Courier", '', 8)
        if df.empty:
            if df.attrs.get('phase1_unavailable'):
                msg = "(no signals - Phase 1 discovery cache was unavailable, stage never scanned. See logs/prewarm_*.log.)"
            else:
                msg = "(no signals)"
            pdf.cell(200, 5, text=msg, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            ranked = df.sort_values(by=score_col, ascending=False) if score_col in df.columns else df
            cols_to_show = [c for c in cols if c in ranked.columns]
            df_str = ranked[cols_to_show].head(5).to_string(index=False)
            for line in df_str.split('\n'):
                pdf.cell(200, 5, text=line.encode('latin-1', 'ignore').decode('latin-1'), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(5)

    stage_errors = results.get('_errors') or {}
    if stage_errors:
        pdf.set_font("Helvetica", 'B', 12)
        pdf.cell(200, 10, text="STAGE FAILURES", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", '', 9)
        for stage, err in stage_errors.items():
            pdf.multi_cell(0, 5, text=f"{stage}: {err}".encode('latin-1', 'ignore').decode('latin-1'))

    if news_summary:
        pdf.ln(5)
        pdf.set_font("Helvetica", 'B', 12)
        pdf.cell(200, 10, text="GEMINI LIVE NEWS ANALYSIS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", '', 10)
        # Same cleanup as export_to_pdf: strip non-breaking spaces/Markdown bold,
        # and skip long unbreakable === / --- dividers that crash fpdf2's line wrapper.
        safe_news = news_summary.replace('\xa0', ' ').replace('**', '').encode('latin-1', 'ignore').decode('latin-1')
        for line in safe_news.split('\n'):
            stripped = line.strip()
            if len(stripped) > 20 and (set(stripped) == {'='} or set(stripped) == {'-'}):
                continue
            try:
                pdf.multi_cell(0, 5, text=line)
            except Exception as e:
                logger.warning(f"⚠️ Skipped rendering a line in EOD PDF news section due to fpdf2 error: {e}")

    try:
        pdf.output(filename)
        logger.info(f"📄 EOD PDF report saved to {filename}")
        return True
    except Exception as e:
        logger.error(f"Failed to save EOD PDF: {e}")
        return False


def fetch_gemini_news(symbols: list) -> str:
    # Isolate the dependency to this function and handle import failures gracefully,
    # mirroring the pattern used for fpdf2.
    genai = install_and_import('google-genai', critical=False)
    if not genai:
        logger.warning("⚠️ Gemini news fetch skipped because 'google-genai' could not be installed/imported.")
        return "⚠️ Gemini library (google-genai) is not available."

    try:
        from google.genai import types
    except (ImportError, AttributeError):
        logger.warning("⚠️ Could not import 'types' from 'google.genai'. The library might be installed but corrupted.")
        return "⚠️ Gemini library is incomplete or corrupted."

    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return "⚠️ GEMINI_API_KEY not found in .env file."
    try:
        # 1. Initialize the new Client (replaces genai.configure)
        client = genai.Client(api_key=api_key)
        
        prompt = (
            "You are an expert Indian Stock Market analyst. "
            f"Find the most recent news headlines or catalysts for these NSE stocks: {', '.join(symbols)}. "
            "Provide exactly one short, punchy sentence per stock highlighting the most relevant recent news. "
            "Format as a clean bulleted list."
        )
        
        # 2. Configure the Google Search tool using the new types module
        config = types.GenerateContentConfig(
            tools=[{"google_search": {}}]
        )

        response = None  # Initialize response to handle cases where the loop fails
        for attempt in range(4):  # 1 initial call + 3 retries
            if shutdown_manager.is_shutdown():
                logger.warning("Shutdown initiated, cancelling Gemini news fetch.")
                return "⚠️ News fetch cancelled due to application shutdown."
            try:
                # 3. New generate_content syntax (also upgrading you to 2.5-flash)
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=config
                )
                break  # Success
            except Exception as e:
                if "503" in str(e) and attempt < 3:
                    base_delay = 5  # seconds
                    wait_time = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    logger.warning(f"Gemini API returned 503, retrying in {wait_time:.2f}s... (Attempt {attempt + 1}/4)")
                    time.sleep(wait_time)
                else:
                    raise  # Re-raise the last exception or if it's not a 503 error

        return response.text.strip() if response and response.text else "⚠️ Gemini returned an empty response."
    except Exception as e:
        logger.error(f"Failed to fetch news from Gemini API: {e}", exc_info=True)
        return "⚠️ Failed to fetch news from Gemini API. See logs for details."
def print_btst_ranking_shift(df_signals: pd.DataFrame):
    if df_signals.empty or 'BTST_Final_Score' not in df_signals.columns:
        return
    before = df_signals.sort_values(by='Decision_Score', ascending=True).head(5) # Note: Decision_Score is inverse, lower is better
    after = df_signals.sort_values(by='BTST_Final_Score', ascending=False).head(5)
    print("\nBTST RANKING (BEFORE - OLD MODEL)")
    print("Based on 'Decision_Score' which penalizes high RS/ADX. Lower is better.")
    before_cols = ['Symbol', 'Decision_Score', 'Score', 'RS_Pctl', 'ADX']
    print(before[[c for c in before_cols if c in before.columns]].to_string(index=False))
    print("\nBTST RANKING (AFTER - NEW EOD MODEL)")
    print("Based on 'BTST_Final_Score' which rewards daily structure. Higher is better.")
    after_cols = ['Symbol', 'BTST_Final_Score', 'Decision_Score', 'BTST_Score']
    print(after[[c for c in after_cols if c in after.columns]].to_string(index=False))

def print_top3_engine(df_signals: pd.DataFrame):
    ranked = add_decision_scores(df_signals)
    if ranked.empty: return
    for horizon in ['BTST', 'SWING', 'INTRADAY', 'GAP']:
        # FIX: Check for column existence before using .astype() to prevent error on a string default.
        if 'Horizon' in ranked.columns:
            subset = ranked[ranked['Horizon'].astype(str).str.upper() == horizon].copy()
        else:
            # If Horizon column doesn't exist, create an empty DataFrame to avoid errors.
            subset = pd.DataFrame(columns=ranked.columns)
        if subset.empty: continue
        subset = subset.sort_values(by='Decision_Score', ascending=True).head(3)
        print(f"\nTOP {horizon}")
        for idx, (_, row) in enumerate(subset.iterrows(), start=1):
            print(f"{idx}. {row['Symbol']} | Confidence {row['Decision_Score']:.1f}/100 | Risk {row['Risk_Level']} | {row['Rank_Reason']}")

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

def _get_rejection_reason(row: pd.Series) -> str:
    """Gets the primary, high-level reason for rejection."""
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

def _format_execution_rejection_details(row: pd.Series) -> str:
    """Creates a detailed multi-line explanation for a rejected trade."""
    breakdown = row.get('_Execution_Score_Breakdown')
    max_points = row.get('_Execution_Max_Points')

    if breakdown is None or max_points is None:
        return f"  └─ Reason: {row.get('Reason', 'Details not available.')}"

    score = row.get('Execution_Score', 0)
    threshold = config.ExecutionThresholds.WATCH_SCORE
    points_missed = threshold - score if score < threshold else 0

    details = []
    potential_promotions = []

    for key, max_p in max_points.items():
        awarded_p = breakdown.get(key, 0)
        if awarded_p < max_p:
            missed_p = max_p - awarded_p
            details.append(f"    - {key.replace('_', ' ').title()}: Missed {missed_p} of {max_p} pts")
            if (score + missed_p) >= threshold:
                potential_promotions.append(key.replace('_', ' ').title())

    rejection_reason = _get_rejection_reason(row)
    
    output = [
        f"  ├─ Execution Score: {score} (Threshold: {threshold}, Missed by: {points_missed})",
        f"  ├─ Rejection Reason: {rejection_reason}",
        f"  └─ Breakdown of Missed Points:"
    ]
    output.extend(details)

    if potential_promotions:
        promo_text = ", ".join(potential_promotions)
        output.append(f"  ✨ NOTE: Improving '{promo_text}' alone could have promoted this to WATCH.")
    else:
        output.append("  ✨ NOTE: Multiple components failed; no single fix would promote to WATCH.")
        
    return "\n".join(output)

def _display_rejection_summary(wait_df: pd.DataFrame):
    """Prints a high-level summary of why trades were rejected."""
    if wait_df.empty or 'Execution_Score' not in wait_df.columns:
        return

    print("\n" + "#" * 50)
    print("🕵️  REJECTION SUMMARY (for WAIT signals)")
    print("#" * 50)
    
    avg_exec_score = wait_df['Execution_Score'].mean()
    watch_threshold = config.ExecutionThresholds.WATCH_SCORE
    avg_missed_pts = (watch_threshold - wait_df['Execution_Score']).clip(lower=0).mean()

    print(f"  Average Execution Score: {avg_exec_score:.1f} / 75")
    print(f"  Average Points Missed for WATCH: {avg_missed_pts:.1f}")
    
    print("\n  Most Common Rejection Reasons:")
    rejection_counts = wait_df['Reason'].value_counts()
    for reason, count in rejection_counts.head(3).items():
        print(f"  - {reason:<30} ({count} stocks)")
    
    print("#" * 50)

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
    print(f"Date:         {datetime.now(config.MARKET_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")
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
            # Default concise mode shows only the one-line reason.
            # The full breakdown is available in the debug report.
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

    # Add the rejection summary at the end of the clean report
    _display_rejection_summary(wait_df)

def _display_debug_report(df_signals, strategy):
    """Clears the console and prints detailed debug tables for the top scan results."""
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\n" + "="*100)
    print(f"🚨 TOP 10 CONFIRMED SETUPS (DEBUG MODE) - {strategy} 🚨")
    if strategy.upper() in ['BTST', 'GAP', 'INTRADAY']:
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

    print("\n--- REJECTION ANALYSIS (TOP 10) ---")
    wait_df = df_signals[df_signals['Execution_Recommendation'].isin(['WAIT', 'NO BUY'])]
    if not wait_df.empty:
        for _, row in wait_df.head(10).iterrows():
            print(f"\n- {row['Symbol']}")
            print(_format_execution_rejection_details(row))
    else:
        print("No rejected signals in this run.")
    print("="*100 + "\n")

def display_confirmation_results(df_signals, strategy, discovered_df):
    """Dispatches to the correct display function based on debug configuration."""
    if df_signals is None or df_signals.empty:
        logger.info("No confirmation signals to report for this run.")
        return
    if config.AppConfig.DEBUG_REPORT:
        _display_debug_report(df_signals, strategy)
    else:
        _display_clean_report(df_signals, strategy, discovered_df)

def run_report_pipeline(df_signals: pd.DataFrame, strategy: str = 'BTST'):
    """Phase 3 reporting is intentionally isolated so reports never delay signal display."""
    if df_signals is None or df_signals.empty or shutdown_manager.is_shutdown():
        if df_signals is None:
            logger.warning("run_report_pipeline received None for df_signals.")
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
        # FIX: Convert the row (a pandas Series) to a dict before passing to log_signal.
        for _, sig_row in df_signals.iterrows():
            if shutdown_manager.is_shutdown(): break
            db.log_signal(sig_row.to_dict())
    finally:
        db.close()  # SignalDB close is now idempotent

    if shutdown_manager.is_shutdown(): return

    pdf_file = f"scan_report_{strategy.upper()}_{datetime.now().strftime('%Y_%m_%d_%H%M')}.pdf"
    export_to_pdf(top_df, news_summary, pdf_file, strategy)
    print(f"📄 Background {strategy.upper()} report saved to {pdf_file}")

def start_background_report(df_signals: pd.DataFrame, strategy: str = 'BTST'):
    """Starts Phase 3 asynchronously after actionable signals are already on screen."""
    worker = threading.Thread(target=run_report_pipeline, args=(df_signals.copy(), strategy), name="ReportGeneratorThread", daemon=True)
    worker.start()
    shutdown_manager.register_worker(worker)
    return worker
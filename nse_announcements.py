"""Tier-1 & Tier-2 catalyst scanner: NSE corporate announcements, cross-referenced
against live technical setups and enriched with Gemini LLM semantic scoring.

- Tier-1 (Fast/Heuristic): Category-based lookup (via announcement_config.yaml)
  plus technical cross-referencing (~0.05s, zero external API cost).
- Tier-2 (Deep/Gemini LLM): Reads the actual filing summary text (attchmntText)
  to score catalyst magnitude (1.0 to 10.0), financial impact, sentiment, and
  extracts a 1-sentence concrete financial takeaway with deal sizes.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import pandas as pd
import requests
import yaml
from dotenv import load_dotenv

import config
from utils import install_and_import

load_dotenv()
logger = logging.getLogger(__name__)

ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
ANNOUNCEMENT_CONFIG_PATH = "announcement_config.yaml"
_DEFAULT_CONFIG = {'high_materiality_categories': [], 'medium_materiality_categories': []}


def _load_config() -> dict:
    if not os.path.exists(ANNOUNCEMENT_CONFIG_PATH):
        logger.warning(f"{ANNOUNCEMENT_CONFIG_PATH} not found; every announcement will read LOW materiality.")
        return _DEFAULT_CONFIG
    try:
        with open(ANNOUNCEMENT_CONFIG_PATH, 'r') as f:
            loaded = yaml.safe_load(f)
        if not isinstance(loaded, dict):
            raise ValueError("top-level YAML content is not a mapping")
        return {**_DEFAULT_CONFIG, **loaded}
    except Exception as e:
        logger.warning(f"Failed to parse {ANNOUNCEMENT_CONFIG_PATH} ({e}); every announcement will read LOW materiality.")
        return _DEFAULT_CONFIG


def fetch_announcements(from_date: pd.Timestamp, to_date: pd.Timestamp, universe_only: bool = True) -> pd.DataFrame:
    """Fetches NSE's market-wide corporate-announcements feed for the given
    date range. `universe_only` filters down to config.Universe.SECTOR_MAP's
    symbols (this app's actual scan universe) - the raw feed runs to
    ~1000+ rows/day market-wide, most of it irrelevant to any stock this
    system ever scans.
    """
    headers = NSE_HEADERS.copy()
    session = requests.Session()
    session.headers.update(headers)
    try:
        resp = session.get(
            ANNOUNCEMENTS_URL,
            params={
                'index': 'equities',
                'from_date': from_date.strftime('%d-%m-%Y'),
                'to_date': to_date.strftime('%d-%m-%Y'),
            },
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        logger.warning(f"NSE announcements fetch failed: {e}")
        return pd.DataFrame()
    finally:
        session.close()

    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    keep_cols = ['symbol', 'sm_name', 'desc', 'attchmntText', 'smIndustry', 'an_dt', 'attchmntFile']
    df = df[[c for c in keep_cols if c in df.columns]].rename(columns={
        'sm_name': 'Company', 'desc': 'Category', 'attchmntText': 'Summary',
        'smIndustry': 'Industry', 'an_dt': 'Announced_At', 'attchmntFile': 'Attachment_URL',
        'symbol': 'Symbol',
    })

    if universe_only:
        universe = set(config.Universe.SECTOR_MAP.keys())
        df = df[df['Symbol'].isin(universe)].reset_index(drop=True)

    return df


def classify_materiality(df: pd.DataFrame, cfg: Optional[dict] = None) -> pd.DataFrame:
    """Adds a Materiality column (HIGH/MEDIUM/LOW) based on NSE's own
    Category field - see announcement_config.yaml."""
    if df.empty:
        return df
    cfg = cfg if cfg is not None else _load_config()
    high = set(cfg.get('high_materiality_categories', []))
    medium = set(cfg.get('medium_materiality_categories', []))

    def _tier(category) -> str:
        if category in high:
            return 'HIGH'
        if category in medium:
            return 'MEDIUM'
        return 'LOW'

    df = df.copy()
    df['Materiality'] = df['Category'].map(_tier)
    return df


def cross_reference_technical_setup(df: pd.DataFrame, report_type: str) -> pd.DataFrame:
    """Adds a Has_Technical_Setup column: True if the announcement's symbol
    also appears in the latest emfb/momentum report - a stock with both a
    fresh catalyst AND an already-live technical setup is the strongest
    combined signal this system can currently produce.
    """
    df = df.copy()
    try:
        from trigger_status import find_latest_report
        report_path = find_latest_report(report_type)
        if report_path is None:
            df['Has_Technical_Setup'] = False
            return df
        report_symbols = set(pd.read_csv(report_path)['Symbol'])
    except Exception as e:
        logger.warning(f"Could not cross-reference against latest {report_type} report: {e}")
        df['Has_Technical_Setup'] = False
        return df

    df['Has_Technical_Setup'] = df['Symbol'].isin(report_symbols)
    return df


def analyze_catalysts_with_gemini(df: pd.DataFrame, max_items: int = 15) -> pd.DataFrame:
    """Tier-2 catalyst evaluation using Gemini LLM.

    Reads announcement text (Summary) to score price/financial impact:
      - AI_Catalyst_Score: float (1.0 to 10.0)
      - AI_Sentiment: str ('BULLISH' | 'BEARISH' | 'NEUTRAL')
      - AI_Magnitude: str ('TRANSFORMATIVE' | 'SIGNIFICANT' | 'MODERATE' | 'ROUTINE')
      - AI_Takeaway: str (Concise 1-sentence financial takeaway)

    Fails soft and returns the original DataFrame if Gemini is unavailable.
    """
    if df.empty:
        return df

    df = df.copy()
    df['AI_Catalyst_Score'] = None
    df['AI_Sentiment'] = None
    df['AI_Magnitude'] = None
    df['AI_Takeaway'] = None

    genai = install_and_import('google-genai', critical=False)
    api_key = os.getenv('GEMINI_API_KEY')
    if not genai or not api_key:
        logger.warning("Gemini API key or google-genai package not available for Tier-2 catalyst analysis.")
        return df

    # Focus on top items (sorted by setup presence and materiality)
    items_to_analyze = df.head(max_items)
    if items_to_analyze.empty:
        return df

    announcement_list = []
    for idx, row in items_to_analyze.iterrows():
        announcement_list.append({
            "id": int(idx),
            "symbol": str(row.get('Symbol', '')),
            "company": str(row.get('Company', '')),
            "category": str(row.get('Category', '')),
            "summary_text": str(row.get('Summary', ''))[:400],
        })

    prompt = (
        "You are a senior Indian equity research and quant market analyst.\n"
        "Evaluate the following corporate announcements disclosed to the National Stock Exchange of India (NSE).\n"
        "Assess the true business and stock price impact for each announcement.\n\n"
        f"Announcements JSON:\n{json.dumps(announcement_list, indent=2)}\n\n"
        "Provide a JSON object containing a 'results' array with one entry per announcement:\n"
        "- 'id': integer (matching input id)\n"
        "- 'symbol': string\n"
        "- 'catalyst_score': float between 1.0 (routine/nominal filing) and 10.0 (transformative multi-crore contract, merger/acquisition, or turnaround > 15-20% revenue/mcap)\n"
        "- 'sentiment': 'BULLISH' | 'BEARISH' | 'NEUTRAL'\n"
        "- 'magnitude': 'TRANSFORMATIVE' | 'SIGNIFICANT' | 'MODERATE' | 'ROUTINE'\n"
        "- 'takeaway': string (one concise sentence with concrete figures, order amounts in ₹ Cr, or business impact)\n\n"
        "Respond ONLY with valid JSON."
    )

    try:
        from google.genai import types
        client = genai.Client(api_key=api_key)
        
        config_kwargs = {}
        try:
            config_kwargs["response_mime_type"] = "application/json"
        except Exception:
            pass

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        )

        raw_text = response.text.strip() if response and response.text else ""
        if not raw_text:
            return df

        # Strip markdown formatting if present
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)

        parsed = json.loads(raw_text)
        results = parsed.get('results', []) if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])

        for res in results:
            item_id = res.get('id')
            if item_id in df.index:
                df.at[item_id, 'AI_Catalyst_Score'] = float(res.get('catalyst_score', 5.0))
                df.at[item_id, 'AI_Sentiment'] = str(res.get('sentiment', 'NEUTRAL')).upper()
                df.at[item_id, 'AI_Magnitude'] = str(res.get('magnitude', 'MODERATE')).upper()
                df.at[item_id, 'AI_Takeaway'] = str(res.get('takeaway', ''))

    except Exception as e:
        logger.warning(f"Gemini catalyst analysis failed: {e}")

    return df


def fetch_symbol_catalysts(symbol: str, lookback_days: int = 7, use_ai: bool = True) -> List[Dict[str, Any]]:
    """Fetches and evaluates recent announcements for a single symbol."""
    symbol = symbol.strip().upper()
    to_date = pd.Timestamp(datetime.now().date())
    from_date = to_date - timedelta(days=lookback_days)

    df = fetch_announcements(from_date, to_date, universe_only=False)
    if df.empty:
        return []

    sym_df = df[df['Symbol'] == symbol].copy()
    if sym_df.empty:
        return []

    sym_df = classify_materiality(sym_df)
    if use_ai and not sym_df.empty:
        sym_df = analyze_catalysts_with_gemini(sym_df, max_items=5)

    return sym_df.to_dict(orient='records')


def build_watchlist(report_type: str = 'emfb', lookback_days: int = 1, use_ai: bool = False) -> pd.DataFrame:
    """End-to-end catalyst pipeline: fetch -> classify -> cross-reference -> optional Gemini scoring."""
    to_date = pd.Timestamp(datetime.now().date()) - pd.Timedelta(days=1)  # yesterday's closed session
    from_date = to_date - timedelta(days=lookback_days - 1)

    df = fetch_announcements(from_date, to_date)
    if df.empty:
        return df
    df = classify_materiality(df)
    df = cross_reference_technical_setup(df, report_type)

    materiality_rank = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
    df['_sort_materiality'] = df['Materiality'].map(materiality_rank)
    df['_sort_setup'] = ~df['Has_Technical_Setup']  # False (has setup) sorts first
    df = df.sort_values(['_sort_materiality', '_sort_setup']).drop(columns=['_sort_materiality', '_sort_setup'])
    df = df.reset_index(drop=True)

    if use_ai:
        df = analyze_catalysts_with_gemini(df, max_items=15)

    return df


def print_watchlist(df: pd.DataFrame, min_materiality: str = 'MEDIUM') -> None:
    if df.empty:
        print("\nNo announcements found for this period.")
        return

    tiers_to_show = {'HIGH': ['HIGH'], 'MEDIUM': ['HIGH', 'MEDIUM'], 'LOW': ['HIGH', 'MEDIUM', 'LOW']}.get(min_materiality, ['HIGH', 'MEDIUM'])
    shown = df[df['Materiality'].isin(tiers_to_show)]

    has_ai = 'AI_Catalyst_Score' in df.columns and df['AI_Catalyst_Score'].notna().any()

    print("\n" + "=" * 110)
    mode_label = " + GEMINI TIER-2 AI SCORING" if has_ai else ""
    print(f"CATALYST WATCHLIST (Materiality >= {min_materiality}{mode_label}) - {len(shown)}/{len(df)} announcements")
    print("=" * 110)

    sentiment_icons = {'BULLISH': '🟢', 'BEARISH': '🔴', 'NEUTRAL': '⚪'}

    both = shown[shown['Has_Technical_Setup']]
    if not both.empty:
        print(f"\n*** CATALYST + LIVE TECHNICAL SETUP ({len(both)}) - strongest combined signal ***")
        for _, row in both.iterrows():
            sym = row['Symbol']
            cat = row['Category']
            mat = row['Materiality']
            if has_ai and pd.notna(row.get('AI_Catalyst_Score')):
                score = row['AI_Catalyst_Score']
                sent = row.get('AI_Sentiment', 'NEUTRAL')
                icon = sentiment_icons.get(sent, '⚪')
                mag = row.get('AI_Magnitude', 'MODERATE')
                takeaway = row.get('AI_Takeaway') or row['Summary'][:100]
                print(f"  {icon} [{mat} | Score {score:.1f}/10 ({mag})] {sym} ({cat}): {takeaway}")
            else:
                print(f"  [{mat}] {sym} ({cat}): {row['Summary'][:120]}")

    catalyst_only = shown[~shown['Has_Technical_Setup']]
    if not catalyst_only.empty:
        print(f"\nCatalyst only, no current technical setup ({len(catalyst_only)}):")
        for _, row in catalyst_only.iterrows():
            sym = row['Symbol']
            cat = row['Category']
            mat = row['Materiality']
            if has_ai and pd.notna(row.get('AI_Catalyst_Score')):
                score = row['AI_Catalyst_Score']
                sent = row.get('AI_Sentiment', 'NEUTRAL')
                icon = sentiment_icons.get(sent, '⚪')
                mag = row.get('AI_Magnitude', 'MODERATE')
                takeaway = row.get('AI_Takeaway') or row['Summary'][:100]
                print(f"  {icon} [{mat} | Score {score:.1f}/10 ({mag})] {sym} ({cat}): {takeaway}")
            else:
                print(f"  [{mat}] {sym} ({cat}): {row['Summary'][:120]}")

    print("=" * 110)
    print("Note: Materiality filters category noise; Gemini AI scores assess true monetary & price impact.")

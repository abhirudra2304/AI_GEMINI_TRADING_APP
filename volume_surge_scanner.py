"""Market-wide volume+price surge detector - the mechanical half of "why
is this stock suddenly moving."

Built 2026-08-11 after checking MCX's real catalyst (a Jefferies analyst
upgrade) turned out to have zero NSE filing behind it - `nse_announcements.py`
alone would never catch a move like that, since broker research notes
aren't filed with the exchange. This module catches the SYMPTOM (unusual
volume+price today, market-wide via bhavcopy - not limited to the curated
219-name scan universe, since the whole point is catching things outside
the usual list too) and cross-references the cheap explanation (NSE
filings) first. What's left over - a real surge with no filing explaining
it - is exactly the MCX-shaped case, flagged as NEEDS_INVESTIGATION for
the expensive step.

Architectural split (important): this module CANNOT do the expensive
step itself. Web search is a tool available to Claude in conversation,
not something a Python script can call. So this module's job ends at
producing the ranked, filing-cross-referenced candidate list - Claude
does the actual web-search investigation for the top NEEDS_INVESTIGATION
names when this runs (in the /loop or on demand), then reports findings
the same way the MCX/Jefferies research was done manually.
"""
import logging
import os
from datetime import datetime
from typing import Optional

import pandas as pd
import yaml

from nse_daily_history import NSEDailyHistory
from nse_announcements import fetch_announcements, classify_materiality

logger = logging.getLogger(__name__)

SURGE_CONFIG_PATH = "volume_surge_config.yaml"
SURGE_OUTPUT_PATH = "volume_surge_candidates.csv"

_DEFAULT_CONFIG = {
    'min_volume_ratio': 3.0, 'min_price_move_pct': 3.0, 'min_turnover_cr': 10.0,
    'baseline_trading_days': 20, 'report_top_n': 30, 'investigate_top_n': 10,
}


def _load_config() -> dict:
    if not os.path.exists(SURGE_CONFIG_PATH):
        logger.warning(f"{SURGE_CONFIG_PATH} not found; using built-in surge-detection defaults.")
        return _DEFAULT_CONFIG
    try:
        with open(SURGE_CONFIG_PATH, 'r') as f:
            loaded = yaml.safe_load(f)
        if not isinstance(loaded, dict):
            raise ValueError("top-level YAML content is not a mapping")
        return {**_DEFAULT_CONFIG, **loaded}
    except Exception as e:
        logger.warning(f"Failed to parse {SURGE_CONFIG_PATH} ({e}); using built-in defaults.")
        return _DEFAULT_CONFIG


def detect_surges(cfg: Optional[dict] = None) -> pd.DataFrame:
    """Market-wide scan: every EQ-series symbol's volume+price move on the
    most recent bhavcopy day, vs its own trailing baseline. Returns rows
    clearing both min_volume_ratio and min_price_move_pct, sorted by
    volume ratio descending, capped to report_top_n."""
    cfg = cfg if cfg is not None else _load_config()
    feed = NSEDailyHistory()
    try:
        days = cfg['baseline_trading_days'] + 1  # +1 for "today"
        history = feed.get_market_wide_history(days)
    finally:
        feed.close()

    if len(history) < 2:
        logger.warning("Not enough trading days resolved for a surge scan.")
        return pd.DataFrame()

    dates_sorted = sorted(history.keys())
    latest_date = dates_sorted[-1]
    prior_date = dates_sorted[-2]
    baseline_dates = dates_sorted[:-1]  # everything except the latest day

    latest_df = history[latest_date]
    prior_df = history[prior_date]

    # Baseline average volume per symbol across the trailing days (excluding today).
    baseline_frames = [history[d]['Volume'] for d in baseline_dates]
    baseline_volume = pd.concat(baseline_frames, axis=1).mean(axis=1)

    rows = []
    for sym in latest_df.index:
        if sym not in baseline_volume.index or sym not in prior_df.index:
            continue
        avg_vol = baseline_volume.loc[sym]
        today_vol = latest_df.loc[sym, 'Volume']
        today_close = latest_df.loc[sym, 'Close']
        prior_close = prior_df.loc[sym, 'Close']
        if avg_vol <= 0 or prior_close <= 0:
            continue

        volume_ratio = today_vol / avg_vol
        price_move_pct = ((today_close - prior_close) / prior_close) * 100
        turnover_cr = (today_vol * today_close) / 1e7

        if (volume_ratio >= cfg['min_volume_ratio']
                and abs(price_move_pct) >= cfg['min_price_move_pct']
                and turnover_cr >= cfg['min_turnover_cr']):
            rows.append({
                'Symbol': sym, 'Volume_Ratio': round(volume_ratio, 2),
                'Price_Move_Pct': round(price_move_pct, 2), 'Close': round(float(today_close), 2),
                'Turnover_Cr': round(turnover_cr, 1), 'Date': latest_date.strftime('%Y-%m-%d'),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values('Volume_Ratio', ascending=False).head(cfg['report_top_n']).reset_index(drop=True)


def cross_reference_filings(surges_df: pd.DataFrame) -> pd.DataFrame:
    """Adds Has_NSE_Filing / NSE_Filing_Category columns by checking
    today's announcements for each surge candidate - the cheap
    explanation, checked before flagging anything for expensive
    investigation."""
    if surges_df.empty:
        return surges_df

    latest_date = pd.Timestamp(surges_df['Date'].iloc[0])
    ann = fetch_announcements(latest_date, latest_date, universe_only=False)
    ann = classify_materiality(ann) if not ann.empty else ann

    df = surges_df.copy()
    df['Has_NSE_Filing'] = False
    df['NSE_Filing_Category'] = ''

    if not ann.empty:
        for idx, row in df.iterrows():
            sym_filings = ann[ann['Symbol'] == row['Symbol']]
            if not sym_filings.empty:
                df.at[idx, 'Has_NSE_Filing'] = True
                # Highest-materiality filing first if there are several.
                tier_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
                best = sym_filings.assign(_r=sym_filings['Materiality'].map(tier_order)).sort_values('_r').iloc[0]
                df.at[idx, 'NSE_Filing_Category'] = f"{best['Materiality']}: {best['Category']}"

    return df


def flag_investigation_candidates(df: pd.DataFrame, investigate_top_n: Optional[int] = None) -> pd.DataFrame:
    """Marks NEEDS_INVESTIGATION=True on the top-N (by volume ratio)
    surges that have no NSE filing explaining them - the MCX-shaped
    cases. These are what Claude should web-search when this report is
    used, not the whole candidate list."""
    if df.empty:
        return df
    cfg = _load_config()
    top_n = investigate_top_n if investigate_top_n is not None else cfg['investigate_top_n']

    df = df.copy()
    df['NEEDS_INVESTIGATION'] = False
    unexplained = df[~df['Has_NSE_Filing']].sort_values('Volume_Ratio', ascending=False).head(top_n)
    df.loc[unexplained.index, 'NEEDS_INVESTIGATION'] = True
    return df


def build_surge_report() -> pd.DataFrame:
    """End-to-end: detect -> cross-reference filings -> flag investigation
    candidates. Saves to disk and returns the DataFrame."""
    cfg = _load_config()
    surges = detect_surges(cfg)
    if surges.empty:
        logger.info("No volume surges cleared the thresholds today.")
        return surges
    surges = cross_reference_filings(surges)
    surges = flag_investigation_candidates(surges, cfg['investigate_top_n'])

    try:
        surges.to_csv(SURGE_OUTPUT_PATH, index=False)
    except OSError as e:
        logger.warning(f"Failed to save {SURGE_OUTPUT_PATH}: {e}")

    return surges


def print_surge_report(df: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print(f"VOLUME SURGE SCAN - market-wide, {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 100)
    if df.empty:
        print("No surges cleared the thresholds today.")
        print("=" * 100)
        return

    explained = df[df['Has_NSE_Filing']]
    unexplained_investigate = df[df['NEEDS_INVESTIGATION']]
    unexplained_rest = df[(~df['Has_NSE_Filing']) & (~df['NEEDS_INVESTIGATION'])]

    cols = ['Symbol', 'Volume_Ratio', 'Price_Move_Pct', 'Close', 'Turnover_Cr']

    if not explained.empty:
        print(f"\n--- EXPLAINED by NSE filing ({len(explained)}) ---")
        print(explained[cols + ['NSE_Filing_Category']].to_string(index=False))

    if not unexplained_investigate.empty:
        print(f"\n--- NEEDS INVESTIGATION - no NSE filing, top {len(unexplained_investigate)} by volume ratio ---")
        print(unexplained_investigate[cols].to_string(index=False))
        print("\n>>> Claude: web-search each of these for news/analyst action before reporting. <<<")

    if not unexplained_rest.empty:
        print(f"\n--- Unexplained, below investigation cutoff ({len(unexplained_rest)}) ---")
        print(unexplained_rest[cols].to_string(index=False))

    print("=" * 100)
    print(f"Full table saved to {SURGE_OUTPUT_PATH}.")

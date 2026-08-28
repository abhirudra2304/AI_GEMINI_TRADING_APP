"""NSE-backed fallback for EarningsAnalyzer's UNKNOWN results.

earnings_manager.py's EarningsAnalyzer sources exclusively from yfinance's
`ticker.calendar`, which has no 'Earnings Date' entry at all for roughly a
third of this app's universe (measured 2026-08-07: 10/30 random symbols),
including liquid, well-covered names like ANGELONE - not just illiquid
micro-caps. A UNKNOWN result there means "Yahoo had nothing", not "no event
risk", but it reads to a report consumer as close to a cleared signal.

This module wraps EarningsAnalyzer.evaluate_risk() unmodified (no edits to
earnings_manager.py) and, only when its result is UNKNOWN, cross-checks
NSE's own public corporate-board-meetings feed - the actual SEBI-mandated
regulatory filing each company submits to the exchange, not a third-party
scrape. Confirmed 2026-08-07 against ANGELONE: NSE had the real 2026-07-15
results board-meeting filing (filed 2026-07-07) that Yahoo had nothing for.

Limitation carried over from the source itself, not fixable here: a
company's *next* board meeting only appears once actually filed (SEBI
requires >=2 working days' notice, though in practice most file 1-2 weeks
ahead) - NSE cannot supply a "next earnings is 86 days away" answer the way
Yahoo occasionally can. This fallback is strongest exactly where it matters
most: confirming imminent earnings (the veto window) and confirming
already-reported results, not predicting a distant future date.
"""
import logging
import re
from datetime import datetime, date
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

NSE_BOARD_MEETINGS_URL = "https://www.nseindia.com/api/corporate-board-meetings"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
FINANCIAL_RESULTS_PATTERN = re.compile(r"financial results", re.IGNORECASE)
_REASON_PREFIX_PATTERN = re.compile(r"^\[[^\]]*\]\s*")


class NSEEarningsFallback:
    """Thin, cached wrapper around NSE's board-meetings endpoint.

    A single requests.Session is reused across calls (connection pooling;
    NSE showed no throttling across 15 rapid sequential calls in testing).
    Per-symbol results are cached for the process lifetime - board-meeting
    filings don't change intraday, same caching assumption
    EarningsAnalyzer already makes for its own Yahoo results.
    """

    def __init__(self, timeout_seconds: float = 10.0):
        self.timeout_seconds = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update(NSE_HEADERS)
        self._cache: dict[str, Optional[date]] = {}
        self._latest_desc_cache: dict[str, str] = {}

    def _fetch_latest_financial_results_date(self, symbol: str) -> tuple[Optional[date], str]:
        """Returns (date, bm_desc) of the most recent Financial Results board
        meeting filing NSE has for `symbol` - whichever is most recent,
        past or future. (None, "") if NSE has nothing relevant or the
        request fails; failures are logged but never raised, matching
        EarningsAnalyzer's own fail-soft contract."""
        try:
            resp = self._session.get(
                NSE_BOARD_MEETINGS_URL,
                params={"index": "equities", "symbol": symbol},
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as e:
            logger.warning(f"NSE board-meetings fallback failed for {symbol}: {e}")
            return None, ""

        if not isinstance(rows, list) or not rows:
            return None, ""

        best_date: Optional[date] = None
        best_desc = ""
        for row in rows:
            purpose = row.get("bm_purpose", "") or ""
            if not FINANCIAL_RESULTS_PATTERN.search(purpose):
                continue
            raw_date = row.get("bm_date")
            if not raw_date:
                continue
            try:
                parsed = datetime.strptime(raw_date, "%d-%b-%Y").date()
            except ValueError:
                continue
            # "Most recent" means latest by calendar date, whether that's a
            # confirmed-future meeting or the last-reported past one -
            # NSE's list isn't guaranteed sorted, so track the max explicitly.
            if best_date is None or parsed > best_date:
                best_date = parsed
                best_desc = row.get("bm_desc", "") or purpose

        return best_date, best_desc

    def get_latest_financial_results_date(self, symbol: str) -> tuple[Optional[date], str]:
        if symbol in self._cache:
            return self._cache[symbol], self._latest_desc_cache.get(symbol, "")
        result_date, desc = self._fetch_latest_financial_results_date(symbol)
        self._cache[symbol] = result_date
        if desc:
            self._latest_desc_cache[symbol] = desc
        return result_date, desc

    def close(self):
        self._session.close()


def verify_earnings_risk(
    symbol: str,
    yahoo_result: dict,
    fallback: NSEEarningsFallback,
    danger_zone_days: int = 5,
) -> dict:
    """Takes EarningsAnalyzer.evaluate_risk()'s output verbatim and, only if
    it's UNKNOWN, tries to resolve it via NSE. Any other result (SAFE,
    ELEVATED, HIGH RISK, CLEARED, or the new-listing hard veto) is returned
    unchanged - Yahoo already answered confidently in those cases, and NSE's
    own gaps (e.g. a genuinely new listing with no filing history either)
    shouldn't override a real answer.

    `danger_zone_days` should be passed the same value the caller's
    EarningsAnalyzer instance was constructed with, so the two sources apply
    identical risk-tier thresholds.
    """
    if "UNKNOWN" not in yahoo_result.get("Earnings_Risk", ""):
        return yahoo_result

    nse_date, nse_desc = fallback.get_latest_financial_results_date(symbol)
    if nse_date is None:
        # NSE has nothing either - the original UNKNOWN now genuinely means
        # "checked two independent sources, neither has it", not "only
        # tried one flaky source".
        return yahoo_result

    today = datetime.now().date()
    if nse_date < today:
        return {
            "Earnings_Date": str(nse_date),
            "Days_To_Earnings": 999,
            "Earnings_Risk": "CLEARED (Already Reported) [NSE fallback]",
        }

    days_to_earnings = (nse_date - today).days
    if days_to_earnings <= danger_zone_days:
        risk_level = "HIGH RISK 🛑 (Imminent Earnings) [NSE fallback]"
    elif days_to_earnings <= 15:
        risk_level = "ELEVATED ⚠️ (Earnings Approaching) [NSE fallback]"
    else:
        risk_level = "SAFE ✅ [NSE fallback]"

    logger.info(
        f"NSE fallback resolved {symbol}'s UNKNOWN earnings status: "
        f"{nse_date} ({risk_level}) - {nse_desc[:100]}"
    )
    return {
        "Earnings_Date": str(nse_date),
        "Days_To_Earnings": days_to_earnings,
        "Earnings_Risk": risk_level,
    }


def apply_nse_earnings_fallback(df: pd.DataFrame) -> pd.DataFrame:
    """Post-processing pass over a ranked EMFB/momentum DataFrame: for every
    row whose Earnings_Risk is UNKNOWN (Yahoo, via EMFBScanner's
    earnings_engine, had nothing), cross-check NSE's board-meetings filing
    feed and correct the fields if NSE has an answer.

    Shared by both emfb.py and momentum_scanner.py so the fix applies
    uniformly to both reports - lives here (not in either caller) so neither
    module has to import the other, and so scanner_engine.py stays
    untouched. EMFB_Score itself never factors earnings risk (see
    rank_and_score_emfb), so correcting these fields after scoring changes
    nothing about ranking - only the risk annotation shown to the reader.
    """
    if df.empty or 'Earnings_Risk' not in df.columns:
        return df

    unknown_mask = df['Earnings_Risk'].astype(str).str.contains('UNKNOWN', na=False)
    if not unknown_mask.any():
        return df

    fallback = NSEEarningsFallback()
    try:
        for idx in df.index[unknown_mask]:
            symbol = df.at[idx, 'Symbol']
            yahoo_result = {
                'Earnings_Date': df.at[idx, 'Earnings_Date'],
                'Days_To_Earnings': df.at[idx, 'Days_To_Earnings'],
                'Earnings_Risk': df.at[idx, 'Earnings_Risk'],
            }
            resolved = verify_earnings_risk(symbol, yahoo_result, fallback)
            if resolved['Earnings_Risk'] == yahoo_result['Earnings_Risk']:
                continue  # NSE had nothing either - left unchanged.

            df.at[idx, 'Earnings_Date'] = resolved['Earnings_Date']
            df.at[idx, 'Days_To_Earnings'] = resolved['Days_To_Earnings']
            df.at[idx, 'Earnings_Risk'] = resolved['Earnings_Risk']

            # Rebuild the Reason prefix to match the corrected status, same
            # prefix vocabulary rank_and_score_emfb itself uses.
            reason = str(df.at[idx, 'Reason']) if 'Reason' in df.columns else ""
            reason_body = _REASON_PREFIX_PATTERN.sub("", reason, count=1)
            if "HIGH RISK" in resolved['Earnings_Risk']:
                new_prefix = f"[🛑 VETO: Earnings in {resolved['Days_To_Earnings']} days. Avoid binary risk!] "
            elif "ELEVATED" in resolved['Earnings_Risk']:
                new_prefix = "[⚠️ Earnings approaching - confirmed via NSE filing] "
            else:  # CLEARED or SAFE
                new_prefix = ""
            if 'Reason' in df.columns:
                df.at[idx, 'Reason'] = new_prefix + reason_body
    finally:
        fallback.close()

    return df

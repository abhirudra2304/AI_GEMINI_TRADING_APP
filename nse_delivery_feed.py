"""Real delivery-volume feed for institutional_flow.py, replacing the
hardcoded 0.0 stub in pipeline_runner.py's _augment_with_delivery_volume.

Confirmed 2026-08-07: NSE publishes a daily "full bhavcopy" CSV (public,
no auth - same category of source as earnings_verifier.py's board-meetings
endpoint) containing DELIV_QTY (shares delivered) and DELIV_PER (delivery
%) for every EQ-series symbol, one file per trading day:

    https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv

One HTTP call covers the *entire* market for that day - fetching N days of
history costs N requests total, not N x (number of symbols), which is why
this is practical despite institutional_flow.py wanting a full time series
rather than just today's snapshot.

Window sizing: institutional_flow.py's InstitutionalFlowConfig only looks
back accumulation_window=20 sessions (the rolling accumulation-footprint
sum) and uses just the latest single day directly in institutional_score -
RVOL's own 50-session window is volume-only, not delivery. ~30 trading
days of bhavcopy is therefore enough for every current consumer; there is
no need to fetch pipeline_runner.py's full 460-calendar-day price history
window for this specifically.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BHAVCOPY_URL_TEMPLATE = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/csv, */*",
}


class NSEDeliveryFeed:
    """Fetches and caches NSE's daily bhavcopy for delivery-quantity data.
    One requests.Session reused across calls; per-date results cached for
    the instance's lifetime (bhavcopy for a closed trading day never
    changes, so this is safe to reuse across an entire pipeline run)."""

    def __init__(self, timeout_seconds: float = 15.0):
        self.timeout_seconds = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update(NSE_HEADERS)
        self._day_cache: dict[str, Optional[pd.DataFrame]] = {}

    def _fetch_day(self, date: pd.Timestamp) -> Optional[pd.DataFrame]:
        """One trading day's bhavcopy, indexed by SYMBOL, with
        DeliveryVolume/Volume columns. None if that date has no file
        (weekend/holiday/not-yet-published) or the request fails - callers
        must treat that as 'no data for this day', not an error."""
        date_str = date.strftime("%d%m%Y")
        if date_str in self._day_cache:
            return self._day_cache[date_str]

        url = BHAVCOPY_URL_TEMPLATE.format(date=date_str)
        try:
            resp = self._session.get(url, timeout=self.timeout_seconds)
            if resp.status_code != 200:
                self._day_cache[date_str] = None
                return None
            df = pd.read_csv(pd.io.common.StringIO(resp.text), skipinitialspace=True)
            df.columns = df.columns.str.strip()
        except Exception as e:
            logger.warning(f"NSE bhavcopy fetch failed for {date_str}: {e}")
            self._day_cache[date_str] = None
            return None

        required = {'SYMBOL', 'SERIES', 'DELIV_QTY', 'TTL_TRD_QNTY'}
        if not required.issubset(df.columns):
            logger.warning(f"Bhavcopy for {date_str} missing expected columns: {required - set(df.columns)}")
            self._day_cache[date_str] = None
            return None

        df['SERIES'] = df['SERIES'].astype(str).str.strip()
        df = df[df['SERIES'] == 'EQ'].copy()
        df['SYMBOL'] = df['SYMBOL'].astype(str).str.strip()
        df['DELIV_QTY'] = pd.to_numeric(df['DELIV_QTY'], errors='coerce').fillna(0.0)
        df['TTL_TRD_QNTY'] = pd.to_numeric(df['TTL_TRD_QNTY'], errors='coerce').fillna(0.0)
        result = df.set_index('SYMBOL')[['DELIV_QTY', 'TTL_TRD_QNTY']]
        result = result[~result.index.duplicated(keep='first')]

        self._day_cache[date_str] = result
        return result

    def fetch_delivery_history(self, symbols: list[str], trading_days: int = 30) -> pd.DataFrame:
        """Wide DataFrame: index=trading date (Timestamp, normalized),
        columns=symbols, values=DeliveryVolume (shares). Walks backward
        from yesterday (today's session, if any, isn't published yet)
        skipping weekends, stopping once `trading_days` successful fetches
        are collected or a 3x calendar-day search cap is hit (covers NSE
        holidays without searching indefinitely on a broken feed).
        """
        symbol_set = set(symbols)
        rows: dict[pd.Timestamp, dict] = {}
        cursor = pd.Timestamp(datetime.now().date()) - timedelta(days=1)
        max_attempts = trading_days * 3
        attempts = 0
        collected = 0

        while collected < trading_days and attempts < max_attempts:
            attempts += 1
            if cursor.weekday() < 5:  # Mon-Fri only; NSE holidays just 404 and get skipped below
                day_df = self._fetch_day(cursor)
                if day_df is not None:
                    present = day_df.index.intersection(symbol_set)
                    rows[cursor] = day_df.loc[present, 'DELIV_QTY'].to_dict()
                    collected += 1
            cursor -= timedelta(days=1)

        if not rows:
            logger.warning("NSE delivery feed: no trading days resolved - returning empty history.")
            return pd.DataFrame(columns=symbols)

        history = pd.DataFrame.from_dict(rows, orient='index').sort_index()
        # Symbols with zero delivery data any day (illiquid, or genuinely
        # missing from that day's bhavcopy) get 0.0, not NaN - same
        # conservative-default philosophy as the stub this replaces.
        history = history.reindex(columns=symbols, fill_value=0.0).fillna(0.0)
        return history

    def close(self):
        self._session.close()

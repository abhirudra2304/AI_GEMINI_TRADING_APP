"""Bhavcopy-backed daily OHLCV source - a persistent, disk-cached
alternative to DataBroker.fetch_ohlcv(interval='ONE_DAY') for the bulk
per-symbol daily-candle fetches that dominate a discovery/momentum scan's
API load.

Motivation (2026-08-10): a Monday-market-open cache refresh hit heavy
Angel rate limiting - BTST discovery alone logged 210 RateLimit errors
(220 retries, 199 cooldowns) fetching ~200 symbols' daily history one
call each. NSE's public bhavcopy (already used by nse_delivery_feed.py
for delivery data) gives the ENTIRE market's daily OHLCV in one free,
unauthenticated HTTP call per trading day - completely independent of
universe size, and with no throttling observed (30 days fetched in 1.5s
during earlier testing).

Key architectural difference from nse_delivery_feed.py: that module
caches per-day data in memory only (one NSEDeliveryFeed instance,
lives and dies with one process). This module persists each trading
day's full-market bhavcopy to disk (bhavcopy_cache/YYYYMMDD.parquet) -
a historical day's data never changes, so once fetched it's cached
forever. Steady-state operation therefore needs only ONE new fetch per
day (today's file) covering the whole universe, not one fetch per symbol
per run.

Output shape matches DataBroker.fetch_ohlcv(...)'s exactly - columns
['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'], Timestamp
tz-aware (config.MARKET_TZ), sorted ascending - so this is a genuine
drop-in for any caller that only needs daily bars.

Scope (deliberate, see project_ias_architecture memory): this only
replaces the per-STOCK universe daily fetch. Index values (Nifty 50,
NIFTY BANK, sector indices) are NOT in the equity bhavcopy (a separate
NSE data product) and still come from the broker. discovery.py and
data_broker.py (V1.0) are untouched - this is wired into
momentum_scanner.py only, which already isn't part of V1.0.
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd
import requests

import config

logger = logging.getLogger(__name__)

BHAVCOPY_URL_TEMPLATE = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/csv, */*",
}
CACHE_DIR = "bhavcopy_cache"


class NSEDailyHistory:
    """Disk-cached full-market daily bhavcopy, sliceable per symbol.

    One requests.Session reused across calls. Each trading day's parsed
    bhavcopy (OHLCV for every EQ-series symbol) is written once to
    `bhavcopy_cache/<YYYYMMDD>.parquet` and never re-fetched - a closed
    trading day's data is immutable.
    """

    def __init__(self, cache_dir: str = CACHE_DIR, timeout_seconds: float = 15.0):
        self.cache_dir = cache_dir
        self.timeout_seconds = timeout_seconds
        os.makedirs(self.cache_dir, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update(NSE_HEADERS)
        self._memo: Dict[str, Optional[pd.DataFrame]] = {}  # date_str -> full-market frame, this-process lifetime

    def _cache_path(self, date_str: str) -> str:
        return os.path.join(self.cache_dir, f"{date_str}.parquet")

    def _load_day(self, date: pd.Timestamp) -> Optional[pd.DataFrame]:
        """Full-market OHLCV for one trading day, indexed by SYMBOL. None
        if that date has no file (weekend/holiday/not-yet-published/fetch
        failure) - callers must treat that as 'no data for this day'."""
        date_str = date.strftime("%Y%m%d")
        if date_str in self._memo:
            return self._memo[date_str]

        disk_path = self._cache_path(date_str)
        if os.path.exists(disk_path):
            try:
                df = pd.read_parquet(disk_path)
                self._memo[date_str] = df
                return df
            except Exception as e:
                logger.warning(f"Failed to read cached bhavcopy {disk_path} ({e}); re-fetching.")

        nse_date_str = date.strftime("%d%m%Y")
        url = BHAVCOPY_URL_TEMPLATE.format(date=nse_date_str)
        try:
            resp = self._session.get(url, timeout=self.timeout_seconds)
            if resp.status_code != 200:
                self._memo[date_str] = None
                return None
            raw = pd.read_csv(pd.io.common.StringIO(resp.text), skipinitialspace=True)
            raw.columns = raw.columns.str.strip()
        except Exception as e:
            logger.warning(f"NSE bhavcopy fetch failed for {nse_date_str}: {e}")
            self._memo[date_str] = None
            return None

        required = {'SYMBOL', 'SERIES', 'OPEN_PRICE', 'HIGH_PRICE', 'LOW_PRICE', 'CLOSE_PRICE', 'TTL_TRD_QNTY'}
        if not required.issubset(raw.columns):
            logger.warning(f"Bhavcopy for {nse_date_str} missing expected columns: {required - set(raw.columns)}")
            self._memo[date_str] = None
            return None

        raw['SERIES'] = raw['SERIES'].astype(str).str.strip()
        raw = raw[raw['SERIES'] == 'EQ'].copy()
        raw['SYMBOL'] = raw['SYMBOL'].astype(str).str.strip()
        df = raw.rename(columns={
            'OPEN_PRICE': 'Open', 'HIGH_PRICE': 'High', 'LOW_PRICE': 'Low',
            'CLOSE_PRICE': 'Close', 'TTL_TRD_QNTY': 'Volume',
        })[['SYMBOL', 'Open', 'High', 'Low', 'Close', 'Volume']]
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['Open', 'High', 'Low', 'Close']).set_index('SYMBOL')
        df = df[~df.index.duplicated(keep='first')]

        try:
            df.to_parquet(disk_path)
        except OSError as e:
            logger.warning(f"Failed to persist bhavcopy cache {disk_path}: {e}")

        self._memo[date_str] = df
        return df

    def ensure_days_cached(self, trading_days: int) -> list[pd.Timestamp]:
        """Backfills (or confirms already-cached) the last `trading_days`
        trading days on disk, walking backward from yesterday (today's
        session, if any, isn't published on bhavcopy yet). Returns the
        list of trading-day Timestamps that resolved to real data, newest
        first - used by fetch_daily_history_bulk to assemble per-symbol
        frames without re-walking dates itself."""
        resolved: list[pd.Timestamp] = []
        cursor = pd.Timestamp(datetime.now().date()) - timedelta(days=1)
        max_attempts = trading_days * 3  # covers NSE holidays without searching forever
        attempts = 0
        while len(resolved) < trading_days and attempts < max_attempts:
            attempts += 1
            if cursor.weekday() < 5:
                day_df = self._load_day(cursor)
                if day_df is not None:
                    resolved.append(cursor)
            cursor -= timedelta(days=1)
        return resolved

    def get_market_wide_history(self, trading_days: int) -> Dict[pd.Timestamp, pd.DataFrame]:
        """Returns the last `trading_days` sessions' full-market frames
        (date -> DataFrame indexed by SYMBOL with Open/High/Low/Close/
        Volume), unfiltered to any symbol list. Unlike
        fetch_daily_history_bulk (which assembles per-symbol series for a
        known watchlist), this is for whole-market scans - e.g.
        volume_surge_scanner.py, which needs every symbol's volume
        history to compute a market-wide baseline, not just symbols
        already in the trading universe."""
        resolved_days = self.ensure_days_cached(trading_days)
        return {d: self._load_day(d) for d in resolved_days if self._load_day(d) is not None}

    def fetch_daily_history_bulk(self, symbols: list[str], days_back: int) -> Dict[str, pd.DataFrame]:
        """Per-symbol daily OHLCV, shaped exactly like
        DataBroker.fetch_ohlcv(symbol, 'ONE_DAY', days_back) returns -
        columns ['Timestamp','Open','High','Low','Close','Volume'],
        Timestamp tz-aware (config.MARKET_TZ), ascending. Symbols with no
        data on a given day simply don't get a row for it (no synthetic
        fill) - same "missing means missing" contract as the broker path.
        `days_back` is trading days here (bhavcopy has no calendar-day
        gaps to skip past), unlike the broker's calendar-day `days_back`.
        """
        trading_days = self.ensure_days_cached(days_back)
        symbol_set = set(symbols)
        rows: Dict[str, list] = {s: [] for s in symbols}

        for date in trading_days:  # newest-first from ensure_days_cached; order doesn't matter, sorted below
            day_df = self._load_day(date)
            if day_df is None:
                continue
            present = day_df.index.intersection(symbol_set)
            ts = pd.Timestamp(date.year, date.month, date.day, tzinfo=config.MARKET_TZ)
            for sym in present:
                r = day_df.loc[sym]
                rows[sym].append({
                    'Timestamp': ts, 'Open': float(r['Open']), 'High': float(r['High']),
                    'Low': float(r['Low']), 'Close': float(r['Close']), 'Volume': float(r['Volume']),
                })

        result = {}
        for sym in symbols:
            if not rows[sym]:
                result[sym] = pd.DataFrame(columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                continue
            df = pd.DataFrame(rows[sym]).sort_values('Timestamp').reset_index(drop=True)
            result[sym] = df
        return result

    def close(self):
        self._session.close()

import os
import time
import json
import random
import logging
import threading
import pyotp
import pandas as pd
from typing import Optional
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
from utils import install_and_import
import config

# Ensure critical dependencies are available, installing them if necessary.
install_and_import('logzero')
install_and_import('websocket-client', 'websocket')
SmartConnect = install_and_import('smartapi-python', 'SmartApi').SmartConnect
from profiler import profiler
from lifecycle_manager import shutdown_manager

logger = logging.getLogger(__name__)

class DataBroker:
    def __init__(self):
        """Initializes the Angel One API session and loads the scrip master."""
        # 1. Automatically calculate the absolute directory where this file actually lives
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        
        # 2. Safely resolve absolute system paths dynamically
        dotenv_path = os.path.join(BASE_DIR, '.env')
        scrip_master_path = os.path.join(BASE_DIR, 'scrip_master.json')
        self.cache_dir = os.path.join(BASE_DIR, config.CacheConfig.HISTORICAL_DATA_DIR)
        self.token_map = pd.DataFrame()
        self.symbol_index = {}
        
        # 3. Load variables explicitly from the dynamic path
        load_dotenv(dotenv_path=dotenv_path)
        
        # 4. Initialize the Angel One Session
        self.api = SmartConnect(api_key=os.getenv('ANGEL_API_KEY'))
        self.api.timeout = 15  # Set an explicit network timeout to prevent indefinite SSL hangs
        self.last_refresh_time = datetime.min # Initialize to datetime.min to force initial session generation
        self.refresh_cooldown_seconds = 300 # 5 minutes cooldown
        self.last_api_call_time = datetime.min
        self.jwt_token = None
        self.feed_token = None
        self.min_api_interval_seconds = 1.1 # Increased safety margin to avoid "exceeding access rate"
        self.rate_limit_lock = threading.Lock()
        self.api_request_lock = threading.Lock()
        self.refresh_token = None
        self._generate_new_session(caller="Initialization")
        shutdown_manager.register(self.close_session)
        
        # 5. Automatically create the cache folder if missing
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # 6. Read the JSON master layout using the absolute path mapping
        try:
            if os.path.exists(scrip_master_path):
                self.token_map = pd.read_json(scrip_master_path)
                if not self.token_map.empty:
                    self.token_map = self.token_map[self.token_map['exch_seg'] == 'NSE']
                    self._build_symbol_index()
        except Exception as e:
            logger.error(f"Failed to load scrip master: {e}")
            self.token_map = pd.DataFrame()
            self.symbol_index = {}

    def close_session(self):
        """Terminates the Angel One API session."""
        if not self.jwt_token:
            return
        try:
            print("Closing API session...")
            logger.info("Terminating Angel One API session.")
            self._call_rest_api(self.api.terminateSession, os.getenv('ANGEL_CLIENT_CODE'))
            self.jwt_token = None
            self.feed_token = None
            self.refresh_token = None
            logger.info("Angel One API session terminated successfully.")
        except Exception as e:
            logger.error(f"Failed to terminate Angel One session: {e}", exc_info=True)

    def _normalize_symbol_key(self, symbol: str) -> str:
        if not isinstance(symbol, str):
            return ''
        normalized = symbol.upper().strip()
        normalized = normalized.replace(' ', '')
        normalized = normalized.replace('.', '')
        normalized = normalized.replace('&', 'AND')
        return normalized

    def _build_symbol_index(self):
        self.symbol_index = {}
        for _, row in self.token_map.iterrows():
            for key in [row.get('symbol', ''), row.get('name', '')]:
                if not isinstance(key, str) or not key.strip():
                    continue
                normalized_key = self._normalize_symbol_key(key)
                if not normalized_key:
                    continue
                self.symbol_index.setdefault(normalized_key, []).append(row)
                if normalized_key.endswith('-EQ'):
                    self.symbol_index.setdefault(normalized_key[:-3], []).append(row)
                if normalized_key.endswith('-BE'):
                    self.symbol_index.setdefault(normalized_key[:-3], []).append(row)

    def _resolve_symbol_row(self, symbol: str):
        if not hasattr(self, 'symbol_index'):
            return pd.DataFrame()
        lookup_key = self._normalize_symbol_key(symbol)
        rows = self.symbol_index.get(lookup_key, [])
        if not rows:
            return pd.DataFrame()
        candidates = [row for row in rows if isinstance(row.get('symbol', ''), str) and row.get('symbol').endswith('-EQ')]
        if not candidates:
            candidates = rows
        return pd.DataFrame(candidates)

    def has_symbol(self, symbol: str) -> bool:
        return not self._resolve_symbol_row(symbol).empty

    def filter_available_symbols(self, universe):
        return [symbol for symbol in universe if self.has_symbol(symbol)]

    def _enforce_api_rate_limit(self):
        """Ensures the minimum interval between API calls is respected in a thread-safe manner."""
        with self.rate_limit_lock:
            wait_start_time = time.monotonic()
            elapsed = (datetime.now() - self.last_api_call_time).total_seconds()
            if elapsed < self.min_api_interval_seconds:
                sleep_duration = self.min_api_interval_seconds - elapsed
                profiler.log_timing('rate_limit_delay', sleep_duration)
                time.sleep(sleep_duration)
            self.last_api_call_time = datetime.now()
            queue_wait_duration = time.monotonic() - wait_start_time
            profiler.log_timing('queue_wait', queue_wait_duration)


    def _call_rest_api(self, api_func, *args, **kwargs):
        """Wrapper for all Angel One REST calls to enforce a global rate limit."""
        self._enforce_api_rate_limit()
        start_time = time.monotonic()
        response = api_func(*args, **kwargs)
        duration = time.monotonic() - start_time
        profiler.log_timing('api_call', duration)
        return response

    def _generate_new_session(self, caller: str = "Unknown"):
        """Handles API login and session generation."""
        profiler.log_request(caller, "Session")
        token = pyotp.TOTP(os.getenv('ANGEL_TOTP_KEY')).now()
        session = self._call_rest_api(
            self.api.generateSession,
            os.getenv('ANGEL_CLIENT_CODE'),
            os.getenv('ANGEL_PASSWORD'),
            token,
        )
        if session and session.get('status') and session.get('data'):
            # Store tokens required for WebSocket streams
            self.jwt_token = session['data'].get('jwtToken')
            self.feed_token = session['data'].get('feedToken')
            self.refresh_token = session['data'].get('refreshToken')
            profiler.log_success()
        else:
            profiler.log_error("SessionGenFailed")


    def _ensure_session(self):
        """Verifies session heartbeat and refreshes if necessary."""
        current_time = datetime.now()
        
        # Only check the profile if the cooldown has passed to save network overhead
        if self.last_refresh_time and (current_time - self.last_refresh_time).total_seconds() < self.refresh_cooldown_seconds:
            logger.debug("Session refresh skipped (cooldown active)")
            return

        try:
            profiler.log_request("Session", "Profile")
            # Current SmartAPI builds require refreshToken for getProfile().
            if self.refresh_token:
                self._call_rest_api(self.api.getProfile, self.refresh_token)
            else:
                # This path may fail on older sessions; the exception block will handle it.
                self._call_rest_api(self.api.getProfile)
            logger.info("Angel One session is active.")
            self.last_refresh_time = current_time 
            profiler.log_success()
            return
        except Exception as e:
            logger.warning(f"Angel One session check failed: {e}. Attempting refresh...")
            profiler.log_error("ProfileCallFailed")


        try:
            self._generate_new_session(caller="SessionRefresh")
            self.last_refresh_time = current_time
            logger.info("Angel One session successfully refreshed.")
        except Exception as e:
            logger.error(f"Angel One session refresh FAILED: {e}")

    def fetch_ohlcv(self, symbol: str, interval: str, days_back: int, force_refresh: bool = False, caller: str = "Unknown", end_date: Optional[datetime] = None) -> pd.DataFrame:
        """
        Fetches historical data with optimized read-through caching.
        For ONE_DAY interval, it performs incremental updates to the cache.
        """
        profiler.log_request(caller, interval, symbol)
        # Sanitize symbol for cache filename consistency.
        normalized_symbol = symbol.replace('-EQ', '').replace('-BE', '')
        file_path = os.path.join(self.cache_dir, f"{normalized_symbol}_{interval}_{days_back}.parquet")
        to_date = end_date or datetime.now()

        if force_refresh and os.path.exists(file_path):
            logger.info(f"Force refresh requested for {symbol}. Deleting existing cache file.")
            os.remove(file_path)

        # 1. Cache validation logic
        if interval == 'ONE_DAY':
            cache_ttl = timedelta(hours=1)
        else:
            cache_ttl = timedelta(seconds=50)

        # If using a historical end_date, caching becomes complex.
        # For simplicity in this context, we bypass the cache for historical runs.
        if end_date is not None:
            force_refresh = True

        if os.path.exists(file_path):
            file_age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(file_path))
            if file_age < cache_ttl and not force_refresh:
                profiler.log_cache_event('hit')
                logger.debug(f"Loading fresh cache for {symbol} {interval}.")
                df = pd.read_parquet(file_path)
                profiler.log_cache_event('load')
                return df

            # 2. Incremental update for stale ONE_DAY cache
            if interval == 'ONE_DAY' and not force_refresh:
                try:
                    profiler.log_cache_event('incremental_update')
                    logger.info(f"Cache for {symbol} is stale. Attempting incremental update.")
                    cached_df = pd.read_parquet(file_path)
                    profiler.log_cache_event('load')
                    
                    if not cached_df.empty and 'Timestamp' in cached_df.columns:
                        last_date = cached_df['Timestamp'].max()
                        start_date_for_api = (last_date + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

                        if start_date_for_api.date() >= to_date.date():
                            logger.info(f"Historical cache for {symbol} is already up-to-date.")
                            os.utime(file_path, None)
                            return cached_df
                        
                        logger.info(f"Fetching new candles for {symbol} from {start_date_for_api.strftime('%Y-%m-%d')}.")
                        from_date = start_date_for_api
                        
                        token_row = self._resolve_symbol_row(symbol)
                        if token_row.empty: token_row = self._resolve_symbol_row(f"{symbol}-EQ")
                        if token_row.empty: token_row = self._resolve_symbol_row(f"{symbol}-BE")
                        if token_row.empty:
                            logger.warning(f"Symbol lookup failed for incremental update on '{symbol}'. Falling back to full refresh.")
                            raise IOError("Symbol lookup failed for incremental update.")

                        res = None
                        for attempt in range(4):
                            try:
                                res = self._call_rest_api(self.api.getCandleData, {
                                    'exchange': 'NSE',
                                    'symboltoken': token_row.iloc[0]['token'],
                                    'interval': interval,
                                    'fromdate': from_date.strftime('%Y-%m-%d %H:%M'),
                                    'todate': to_date.strftime('%Y-%m-%d %H:%M')
                                })
                                if isinstance(res, dict) and res.get('status') is False:
                                    if "AB1021" in res.get('message', ''): profiler.log_error('AB1021')
                                    raise Exception(f"API error for {symbol}: {res.get('message')}")
                                profiler.log_success()
                                break
                            except Exception as e:
                                profiler.log_retry()
                                if attempt < 3:
                                    time.sleep(3 * (attempt + 1))
                                    continue
                                logger.error(f"Failed to fetch incremental data for {symbol}: {e}")
                                res = None

                        new_candles_df = pd.DataFrame()
                        if res and res.get('status') and res.get('data'):
                            new_candles_df = pd.DataFrame(res['data'], columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                            new_candles_df['Timestamp'] = pd.to_datetime(new_candles_df['Timestamp'])
                            new_candles_df[['Open', 'High', 'Low', 'Close', 'Volume']] = new_candles_df[['Open', 'High', 'Low', 'Close', 'Volume']].apply(pd.to_numeric)

                        if not new_candles_df.empty:
                            initial_count = len(cached_df)
                            combined_df = pd.concat([cached_df, new_candles_df], ignore_index=True)
                            combined_df.drop_duplicates(subset=['Timestamp'], keep='last', inplace=True)
                            combined_df.sort_values(by='Timestamp', inplace=True)
                            combined_df.reset_index(drop=True, inplace=True)
                            
                            downloaded_count = len(combined_df) - initial_count
                            
                            if downloaded_count > 0:
                                combined_df.to_parquet(file_path, compression='snappy')
                                profiler.log_cache_event('save')
                                print(f"\n[Cache] Loaded {initial_count}, Downloaded {downloaded_count}, Saved {len(combined_df)}")
                            else:
                                logger.info(f"No new candles found for {symbol}. Cache is current.")
                                os.utime(file_path, None)

                            return combined_df
                        else:
                            logger.info(f"No new candles downloaded for {symbol}. Cache is considered current.")
                            os.utime(file_path, None)
                            return cached_df
                except Exception as e:
                    logger.warning(f"Could not incrementally update cache for {symbol}. Performing a full refresh. Error: {e}")
                    pass

        # 3. Full refresh logic (original logic as fallback)
        profiler.log_cache_event('miss' if not os.path.exists(file_path) else 'full_refresh')
        logger.info(f"Performing full data fetch for {symbol} ({days_back} days).")
        from_date = to_date - timedelta(days=days_back)
        
        token_row = pd.DataFrame()
        if symbol.lower() in ['nifty 50', 'nifty', 'nifty50']:
            nifty_variations = ['Nifty 50', 'NIFTY', 'NIFTY50']
            for var in nifty_variations:
                token_row = self._resolve_symbol_row(var)
                if not token_row.empty: break
        else:
            token_row = self._resolve_symbol_row(symbol)
            if token_row.empty: token_row = self._resolve_symbol_row(f"{symbol}-EQ")
            if token_row.empty: token_row = self._resolve_symbol_row(f"{symbol}-BE")

        if token_row.empty:
            logger.warning(f"Symbol lookup failed for full refresh on '{symbol}'.")
            return pd.DataFrame()

        res = None
        for attempt in range(4):
            try:
                res = self._call_rest_api(self.api.getCandleData, {
                    'exchange': 'NSE',
                    'symboltoken': token_row.iloc[0]['token'],
                    'interval': interval,
                    'fromdate': from_date.strftime('%Y-%m-%d %H:%M'),
                    'todate': to_date.strftime('%Y-%m-%d %H:%M')
                })
                if isinstance(res, (bytes, bytearray, str)):
                     raise Exception("Rate limit or unexpected response format.")
                if isinstance(res, dict) and res.get('status') is False:
                    if "AB1021" in res.get('message', ''): profiler.log_error('AB1021')
                    raise Exception(f"API error: {res.get('message')}")
                profiler.log_success()
                break
            except Exception as e:
                profiler.log_retry()
                logger.warning(f"Full fetch failed for {symbol} (Attempt {attempt+1}): {e}")
                if attempt < 3:
                    sleep_time = 3 * (attempt + 1) + random.uniform(1,2)
                    profiler.log_cooldown()
                    time.sleep(sleep_time)
                    continue
                return pd.DataFrame()

        if res and res.get('status') and res.get('data'):
            df = pd.DataFrame(res['data'], columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
            df['Timestamp'] = pd.to_datetime(df['Timestamp'])
            df[['Open', 'High', 'Low', 'Close', 'Volume']] = df[['Open', 'High', 'Low', 'Close', 'Volume']].apply(pd.to_numeric)
            df.to_parquet(file_path, compression='snappy')
            profiler.log_cache_event('save')
            logger.info(f"Saved {len(df)} candles to cache for {symbol}.")
            # For full refresh, mimic the final output format
            if interval == 'ONE_DAY':
                 print(f"\n[Cache] Loaded 0, Downloaded {len(df)}, Saved {len(df)}")
            return df
            
        return pd.DataFrame()

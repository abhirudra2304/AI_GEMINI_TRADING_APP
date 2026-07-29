import os
import time
import json
import random
import logging
from collections import defaultdict
import threading
import pyotp
import pandas as pd
from typing import Optional, Dict, Any, List, Callable
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
from utils import install_and_import
import config

# Ensure critical dependencies are available, installing them if necessary.
# NOTE: the installable pip package is "smartapi-python", but the importable
# module it ships is capitalized "SmartApi" (see utils.IMPORT_NAME_MAP) - a
# plain `from smartapi... import ...` (lowercase) raises ModuleNotFoundError
# even when the package is installed correctly. The V2 websocket class is
# also named SmartWebSocketV2, not SmartSocketV2 - SmartApi/__init__.py only
# re-exports SmartConnect and the older v1 SmartWebSocket at the top level,
# so both classes must be pulled from their actual submodules.
install_and_import('logzero')
install_and_import('websocket-client')

_smartconnect_module = install_and_import('smartapi-python', import_name='SmartApi.smartConnect')
_smartwebsocket_module = install_and_import('smartapi-python', import_name='SmartApi.smartWebSocketV2')
if _smartconnect_module is None or _smartwebsocket_module is None:
    # install_and_import(critical=True) (the default) exits the process on failure,
    # so this only guards the type-narrowing for static analysis - it should be
    # unreachable at runtime.
    raise ImportError("Failed to import the smartapi-python package.")
SmartConnect = _smartconnect_module.SmartConnect
SmartWebSocketV2 = _smartwebsocket_module.SmartWebSocketV2

from profiler import profiler
from lifecycle_manager import shutdown_manager

logger = logging.getLogger(__name__)

# --- WebSocket Tick Aggregation ---

BROKER_TO_AGGREGATOR_INTERVAL = {
    'ONE_MINUTE': '1MIN',
    'FIVE_MINUTE': '5MIN',
    'FIFTEEN_MINUTE': '15MIN',
}
AGGREGATOR_INTERVALS = {
    '1MIN': 1,
    '5MIN': 5,
    '15MIN': 15,
}

def _scalar_to_float(value: Any) -> float:
    """
    Converts a pandas `.loc[...]` scalar cell to a plain float.

    pandas types scalar cell reads as the broad `Scalar` union (which includes
    `complex`, `Timestamp`, etc., since it can't know the column's real dtype
    statically), but `float()`'s own signature rejects `complex` - rightly,
    since `float(complex(1, 2))` really does raise at runtime. `value: Any` is
    the correct boundary type here, not a lazy escape hatch: this function's
    entire job is converting out of that type-erased pandas boundary into a
    concrete float, with an explicit runtime guard against the one case
    (`complex`) that would otherwise raise inside float() itself.
    """
    if isinstance(value, complex):
        raise TypeError(f"Cannot convert complex value {value!r} to float")
    return float(value)


class TickAggregator:
    """
    In-memory aggregator to build OHLCV candles from live WebSocket ticks.
    This class is thread-safe.
    """
    def __init__(self, intervals: Optional[Dict[str, int]] = None):
        self.intervals = intervals or AGGREGATOR_INTERVALS
        # Structure: { '1MIN': { 'token': pd.DataFrame }, '15MIN': { 'token': pd.DataFrame } }
        self.candles = {key: defaultdict(pd.DataFrame) for key in self.intervals}
        self.token_to_symbol = {} # Map token back to symbol for easier access
        self.lock = threading.Lock()

    def register_token(self, token: str, symbol: str):
        with self.lock:
            self.token_to_symbol[str(token)] = symbol

    def _get_candle_start(self, ts: datetime, minute_interval: int) -> datetime:
        """Calculates the start of the candle time bucket."""
        return ts.replace(minute=ts.minute // minute_interval * minute_interval, second=0, microsecond=0)

    def on_tick(self, tick: Dict[str, Any]):
        """Processes a single tick to update the corresponding OHLCV candles."""
        token = str(tick.get('token'))
        ltp_raw = tick.get('ltp')
        volume_raw = tick.get('volume')
        ltt_raw = tick.get('ltt')  # last traded timestamp (epoch)

        # Explicit None/type guards (not `if not all([...])`) so every downstream
        # use of ltp/volume/ltt below is provably a float, never Any | None.
        if ltp_raw is None or volume_raw is None or ltt_raw is None or not token:
            return
        try:
            ltp = float(ltp_raw)
            volume = float(volume_raw)
            ltt = float(ltt_raw)
        except (TypeError, ValueError):
            logger.warning(f"Tick for token {token} had non-numeric ltp/volume/ltt: {tick}")
            return
        try:
            ts = datetime.fromtimestamp(ltt, tz=config.MARKET_TZ)
        except (ValueError, TypeError, OSError):
            return

        with self.lock:
            for interval_name, minute_interval in self.intervals.items():
                candle_start = self._get_candle_start(ts, minute_interval)
                candle_df = self.candles[interval_name][token]

                if candle_df.empty or candle_df['Timestamp'].iloc[-1] != candle_start:
                    new_candle = pd.DataFrame([{'Timestamp': candle_start, 'Open': ltp, 'High': ltp, 'Low': ltp, 'Close': ltp, 'Volume': volume}])
                    self.candles[interval_name][token] = pd.concat([candle_df, new_candle], ignore_index=True)
                else:
                    idx = candle_df.index[-1]
                    current_high = _scalar_to_float(candle_df.loc[idx, 'High'])
                    current_low = _scalar_to_float(candle_df.loc[idx, 'Low'])
                    current_volume = _scalar_to_float(candle_df.loc[idx, 'Volume'])
                    self.candles[interval_name][token].loc[idx, 'High'] = max(current_high, ltp)
                    self.candles[interval_name][token].loc[idx, 'Low'] = min(current_low, ltp)
                    self.candles[interval_name][token].loc[idx, 'Close'] = ltp
                    self.candles[interval_name][token].loc[idx, 'Volume'] = current_volume + volume

    def get_candles(self, token: str, interval: str) -> pd.DataFrame:
        """Retrieves the aggregated candle DataFrame for a given token and interval."""
        with self.lock:
            return self.candles.get(interval, {}).get(str(token), pd.DataFrame()).copy()

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
        # --- NEW WebSocket and Aggregator state ---
        self.aggregator = TickAggregator(intervals=AGGREGATOR_INTERVALS)
        self.ws = None
        self.ws_thread = None
        self.subscribed_tokens = set()
        self.subscription_lock = threading.Lock()
        self._generate_new_session(caller="Initialization")
        shutdown_manager.register(self.close_session)
        shutdown_manager.register(self.close_websocket)
        
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

        # 7. Load the lightweight symbol->token map for the rolling-beta pipeline
        # (see update_tokens.py / calculate_rolling_beta). Missing/stale is not
        # fatal - calculate_rolling_beta falls back to config.Universe.BETA_REGISTRY.
        self.angel_tokens_path = os.path.join(BASE_DIR, 'angel_tokens.json')
        self.angel_tokens: Dict[str, str] = {}
        self._load_angel_tokens()

        # 8. Apply today's persisted live-beta refresh (see refresh_beta_registry)
        # to config.Universe.BETA_REGISTRY, if one exists. This is what lets a
        # `python main.py refresh-beta` run earlier today reach every later,
        # separate `main.py` process without each of them re-hitting the API.
        self.live_beta_cache_path = os.path.join(BASE_DIR, 'beta_registry_live.json')
        self._load_live_beta_overlay()

    def _load_angel_tokens(self) -> None:
        if not os.path.exists(self.angel_tokens_path):
            logger.warning(
                f"'{self.angel_tokens_path}' not found. Run update_tokens.py to enable "
                "rolling beta; calculate_rolling_beta will fall back to BETA_REGISTRY until then."
            )
            return
        try:
            with open(self.angel_tokens_path, 'r') as f:
                self.angel_tokens = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load '{self.angel_tokens_path}': {e}")
            self.angel_tokens = {}

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

    # --- WebSocket Integration Methods ---
    def _on_open(self, wsapp):
        logger.info("✅ WebSocket connection opened.")
        if self.subscribed_tokens:
            tokens_to_resubscribe = list(self.subscribed_tokens)
            logger.info(f"Resubscribing to {len(tokens_to_resubscribe)} tokens...")
            self._send_subscription(tokens_to_resubscribe)

    def _on_data(self, wsapp, data):
        """
        IMPORTANT: This is a placeholder for the complex byte parsing required by SmartAPIv2.
        The actual implementation requires `struct.unpack` based on Angel One's docs.
        For this example, we assume a helper function `parse_tick_data` exists.
        """
        # In a real scenario, you would parse the byte array `data` here.
        # e.g., parsed_tick = self.parse_tick_data(data)
        # self.aggregator.on_tick(parsed_tick)
        # For now, we log that data is being received.
        # logger.debug(f"Raw WS data received: {data}")
        pass # Replace with actual parsing and self.aggregator.on_tick(parsed_data)

    def _on_error(self, wsapp, error):
        logger.error(f"WebSocket error: {error}")

    def _on_close(self, wsapp, close_status_code, close_msg):
        logger.warning(f"WebSocket connection closed: {close_status_code} - {close_msg}")

    def connect_websocket(self):
        if self.ws and self.ws.is_connected():
            logger.info("WebSocket is already connected.")
            return
        self._ensure_session()
        if not all([self.jwt_token, os.getenv('ANGEL_API_KEY'), os.getenv('ANGEL_CLIENT_CODE'), self.feed_token]):
            logger.error("Cannot connect to WebSocket, missing authentication details.")
            return

        self.ws = SmartWebSocketV2(self.jwt_token, os.getenv('ANGEL_API_KEY'), os.getenv('ANGEL_CLIENT_CODE'), self.feed_token)
        self.ws.on_open = self._on_open
        self.ws.on_data = self._on_data
        self.ws.on_error = self._on_error
        self.ws.on_close = self._on_close
        self.ws_thread = threading.Thread(target=self.ws.connect, daemon=True, name="WebSocketThread")
        self.ws_thread.start()
        logger.info("WebSocket connection thread started.")

    def close_websocket(self):
        if self.ws and self.ws.is_connected():
            print("Closing WebSocket...")
            logger.info("Closing WebSocket connection...")
            self.ws.close()
            if self.ws_thread and self.ws_thread.is_alive():
                self.ws_thread.join(timeout=5)
            logger.info("WebSocket connection closed.")

    def _send_subscription(self, token_list: list):
        if not self.ws or not self.ws.is_connected():
            logger.warning("WebSocket not connected. Cannot send subscription.")
            return
        token_json = [{"exchangeType": 1, "tokens": token_list}] # 1 for NSE
        self.ws.subscribe("scanner_subscription", 1, token_json) # mode 1 for LTP
        logger.info(f"Sent subscription request for {len(token_list)} tokens.")

    def subscribe_to_symbols(self, symbols: list):
        with self.subscription_lock:
            # `if row` on a pandas Series is a real runtime bug, not just a type-checker
            # nag: Series.__bool__() raises ValueError("truth value of a Series is
            # ambiguous") for anything but a single-element Series, and a scrip-master
            # row always has multiple columns. Must narrow with `is not None` instead.
            tokens_to_subscribe = [
                row['token'] for symbol in symbols
                if (row := self._resolve_symbol_row(symbol)) is not None and row['token'] not in self.subscribed_tokens
            ]
            if tokens_to_subscribe:
                logger.info(f"Subscribing to {len(tokens_to_subscribe)} new symbols.")
                self._send_subscription(tokens_to_subscribe)
                for token in tokens_to_subscribe: self.subscribed_tokens.add(token)
            else:
                logger.info("All requested symbols are already subscribed.")

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

    def _resolve_symbol_row(self, symbol: str) -> Optional[pd.Series]:
        """Resolves a symbol to its definitive scrip master row (as a Series)."""
        if not hasattr(self, 'symbol_index') or not self.symbol_index:
            return None

        # Handle common index names first
        s_lower = symbol.lower()
        if s_lower in ['nifty 50', 'nifty', 'nifty50']:
            symbol = 'Nifty 50'
        elif s_lower in ['nifty bank', 'banknifty', 'bank nifty']:
            symbol = 'NIFTY BANK'
        elif s_lower in ['india vix', 'vix']:
            symbol = 'India VIX'

        lookup_key = self._normalize_symbol_key(symbol)
        
        # Try direct lookup first
        rows = self.symbol_index.get(lookup_key, [])
        if not rows:
            return None

        # Prefer '-EQ' series for equities, otherwise return the first match.
        for row in rows:
            if isinstance(row.get('symbol', ''), str) and row.get('symbol').endswith('-EQ'):
                return row
        return rows[0]

    def has_symbol(self, symbol: str) -> bool:
        return self._resolve_symbol_row(symbol) is not None

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


    def _call_rest_api(self, api_func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Wrapper for all Angel One REST calls to enforce a global rate limit.

        Returns whatever the SmartAPI SDK returns, which is untyped and not
        guaranteed to be a dict (it can be bytes/str on a malformed response,
        or raise outright on a network failure). This wrapper deliberately
        does NOT catch that exception - every call site already wraps this in
        its own retry loop, and swallowing the error here would break that
        retry logic. Type-narrowing the return value (isinstance(res, dict)
        before touching .get(...)) is each caller's responsibility; this
        method's contract is only "rate-limited call, unknown-shaped result."
        """
        self._enforce_api_rate_limit()
        start_time = time.monotonic()
        response = api_func(*args, **kwargs)
        duration = time.monotonic() - start_time
        profiler.log_timing('api_call', duration)
        return response

    def _generate_new_session(self, caller: str = "Unknown") -> None:
        """Handles API login and session generation."""
        profiler.log_request(caller, "Session")
        # os.getenv() is Optional[str]; pyotp.TOTP() requires a str, so provide
        # a safe "" default. An empty secret will fail TOTP generation loudly
        # (caught below), which is preferable to a silent None-typed crash.
        totp_secret = os.getenv('ANGEL_TOTP_KEY') or ""
        client_code = os.getenv('ANGEL_CLIENT_CODE') or ""
        password = os.getenv('ANGEL_PASSWORD') or ""
        if not totp_secret or not client_code or not password:
            logger.error("Missing ANGEL_TOTP_KEY/ANGEL_CLIENT_CODE/ANGEL_PASSWORD env vars; cannot generate a session.")
            profiler.log_error("SessionGenFailed")
            return

        try:
            token = pyotp.TOTP(totp_secret).now()
        except Exception as e:
            logger.error(f"Failed to generate TOTP token: {e}")
            profiler.log_error("SessionGenFailed")
            return

        session = self._call_rest_api(self.api.generateSession, client_code, password, token)

        # Defensive API handling: never index into the response before confirming
        # its shape - the SDK can return bytes/str/None on a malformed response.
        if not isinstance(session, dict):
            logger.error(f"Unexpected session generation response type: {type(session)} | Response: {session}")
            profiler.log_error("SessionGenFailed")
            return

        session_data = session.get('data')
        if not session.get('status') or not isinstance(session_data, dict):
            logger.error(f"Session generation returned no usable data: {session}")
            profiler.log_error("SessionGenFailed")
            return

        # Store tokens required for WebSocket streams
        self.jwt_token = session_data.get('jwtToken')
        self.feed_token = session_data.get('feedToken')
        self.refresh_token = session_data.get('refreshToken')
        profiler.log_success()


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
        elif interval == 'ONE_HOUR':
            # Long enough that hitting the incremental-update path below (cheap:
            # one API call for just the missing candles) is worthwhile instead
            # of thrashing on every call within a scan; short enough that the
            # current trading day's latest hourly candle doesn't go stale for
            # an entire EOD run. FIFTEEN_MINUTE/FIVE_MINUTE keep the tighter 50s
            # TTL below - LiveScanner's 60s loop genuinely needs that freshness,
            # and (unlike ONE_DAY/ONE_HOUR) there's no incremental path for them.
            cache_ttl = timedelta(minutes=10)
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

            # 2. Incremental update for stale ONE_DAY/ONE_HOUR cache. Fetches only
            # the candles newer than what's cached instead of redownloading the
            # full days_back window - e.g. for ONE_HOUR this avoids re-pulling
            # 100 days of hourly candles just because the 10-minute TTL lapsed.
            if interval in ('ONE_DAY', 'ONE_HOUR') and not force_refresh:
                try:
                    profiler.log_cache_event('incremental_update')
                    logger.info(f"Cache for {symbol} is stale. Attempting incremental update.")
                    cached_df = pd.read_parquet(file_path)
                    profiler.log_cache_event('load')

                    if not cached_df.empty and 'Timestamp' in cached_df.columns:
                        last_date = cached_df['Timestamp'].max()
                        if interval == 'ONE_DAY':
                            # Next full calendar day - daily candles are one-per-day,
                            # so there's nothing "partial" about today's row once it exists.
                            start_date_for_api = (last_date + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                            is_up_to_date = start_date_for_api.date() >= to_date.date()
                        else:  # ONE_HOUR
                            # Just past the last cached candle's own timestamp - unlike
                            # ONE_DAY there's no "start of next period" rounding needed,
                            # the broker only ever returns candles strictly after fromdate.
                            start_date_for_api = last_date + timedelta(hours=1)
                            is_up_to_date = start_date_for_api >= to_date

                        if is_up_to_date:
                            logger.info(f"Historical cache for {symbol} is already up-to-date.")
                            os.utime(file_path, None)
                            return cached_df

                        logger.info(f"Fetching new candles for {symbol} from {start_date_for_api.strftime('%Y-%m-%d %H:%M')}.")
                        from_date = start_date_for_api
                        
                        token_row = self._resolve_symbol_row(symbol)
                        if token_row is None: token_row = self._resolve_symbol_row(f"{symbol}-EQ")
                        if token_row is None: token_row = self._resolve_symbol_row(f"{symbol}-BE")
                        if token_row is None:
                            logger.warning(f"Symbol lookup failed for incremental update on '{symbol}'. Falling back to full refresh.")
                            raise IOError("Symbol lookup failed for incremental update.")

                        res = None
                        fetch_failed = False
                        for attempt in range(4):
                            try:
                                res = self._call_rest_api(self.api.getCandleData, {
                                    'exchange': 'NSE',
                                    'symboltoken': token_row['token'],
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
                                if "429" in str(e) or "503" in str(e): profiler.log_error("RateLimit")
                                profiler.log_retry()
                                if attempt < 3:
                                    wait_time = (2 ** attempt) + random.uniform(0, 1) # Exponential backoff
                                    logger.warning(f"API call for {symbol} failed. Retrying in {wait_time:.2f}s... (Attempt {attempt + 1}/4)")
                                    time.sleep(wait_time)
                                    continue
                                logger.error(f"Failed to fetch incremental data for {symbol}: {e}")
                                res = None
                                # All 4 attempts exhausted with no successful API response - this is a
                                # genuine failure, not a confirmed "no new candles" result. Tracked
                                # separately so the branch below doesn't stamp stale cached_df as fresh
                                # (see incident 2026-07-29: a rate-limited mass-fetch silently served
                                # 216/220 tickers' day-old cache as "current", which pipeline_runner.py's
                                # RS calc then NaN'd out for missing today's bar).
                                fetch_failed = True

                        new_candles_df = pd.DataFrame()
                        # FIX: Ensure the response is a dictionary before accessing keys.
                        if isinstance(res, dict) and res.get('status') and res.get('data'):
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
                        elif fetch_failed:
                            # Don't touch the mtime here: doing so would re-arm the TTL and cause
                            # this stale cache to be served as "fresh" for another cache_ttl window
                            # to every future caller, not just this one - masking a real API/rate-limit
                            # failure as a confirmed up-to-date cache.
                            logger.error(f"Incremental update for {symbol} failed after retries; serving stale cache (last bar {last_date}) without refreshing its TTL.")
                            return cached_df
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
        
        token_row = None
        if symbol.lower() in ['nifty 50', 'nifty', 'nifty50']:
            nifty_variations = ['Nifty 50', 'NIFTY', 'NIFTY50']
            for var in nifty_variations:
                token_row = self._resolve_symbol_row(var)
                if token_row is not None: break
        else:
            token_row = self._resolve_symbol_row(symbol)
            if token_row is None: token_row = self._resolve_symbol_row(f"{symbol}-EQ")
            if token_row is None: token_row = self._resolve_symbol_row(f"{symbol}-BE")

        if token_row is None:
            logger.warning(f"Symbol lookup failed for full refresh on '{symbol}'.")
            return pd.DataFrame()

        res = None
        for attempt in range(4):
            try:
                res = self._call_rest_api(self.api.getCandleData, {
                    'exchange': 'NSE',
                    'symboltoken': token_row['token'],
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
                if "429" in str(e) or "503" in str(e): profiler.log_error("RateLimit")
                profiler.log_retry()
                logger.warning(f"Full fetch failed for {symbol} (Attempt {attempt+1}): {e}")
                if attempt < 3:
                    sleep_time = (2 ** attempt) + random.uniform(0, 1) # Exponential backoff
                    profiler.log_cooldown()
                    time.sleep(sleep_time)
                    continue
                return pd.DataFrame()

        # FIX: Ensure the response is a dictionary before accessing keys.
        if isinstance(res, dict) and res.get('status') and res.get('data'):
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

    def fetch_daily_candles(self, symbol: str, days_back: int = 120) -> pd.DataFrame:
        """
        Fetches ONE_DAY candles for `symbol` directly from Angel One, resolving the
        token via angel_tokens.json (see update_tokens.py). Uninvolved with the
        parquet cache in fetch_ohlcv - this is a thin, uncached path purpose-built
        for calculate_rolling_beta. Returns an empty DataFrame on any failure;
        callers must treat that as "no data" and fall back accordingly.
        """
        token = self.angel_tokens.get(symbol)
        if token is None:
            logger.warning(f"No token for '{symbol}' in angel_tokens.json.")
            return pd.DataFrame()

        to_date = datetime.now()
        from_date = to_date - timedelta(days=days_back)

        # Angel's historical-candle endpoint transiently rejects calls made too
        # soon after login/another call ("Access denied because of exceeding
        # access rate") even when _enforce_api_rate_limit's spacing is respected -
        # mirrors fetch_ohlcv's retry/backoff around the same error.
        res = None
        for attempt in range(4):
            try:
                res = self._call_rest_api(self.api.getCandleData, {
                    'exchange': 'NSE',
                    'symboltoken': token,
                    'interval': 'ONE_DAY',
                    'fromdate': from_date.strftime('%Y-%m-%d %H:%M'),
                    'todate': to_date.strftime('%Y-%m-%d %H:%M'),
                })
                if isinstance(res, (bytes, bytearray, str)):
                    raise Exception("Rate limit or unexpected response format.")
                if isinstance(res, dict) and res.get('status') is False:
                    raise Exception(f"API error: {res.get('message')}")
                break
            except Exception as e:
                logger.warning(f"getCandleData failed for '{symbol}' (attempt {attempt + 1}/4): {e}")
                res = None
                if attempt < 3:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))

        if not isinstance(res, dict) or not res.get('status') or not res.get('data'):
            logger.warning(f"No candle data returned for '{symbol}': {res}")
            return pd.DataFrame()

        df = pd.DataFrame(res['data'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric)
        return df.set_index('timestamp')

    def calculate_rolling_beta(
        self,
        symbol: str,
        benchmark_symbol: str = 'NIFTY_50',
        lookback_days: int = 90,
        _benchmark_returns: Optional[pd.Series] = None,
    ) -> float:
        """
        Computes `symbol`'s beta vs `benchmark_symbol` over the trailing
        `lookback_days` of daily returns, clipped to [0.5, 2.5]. Falls back to
        config.Universe.BETA_REGISTRY.get(symbol, 1.0) whenever live data is
        missing, too short, or degenerate (zero-variance benchmark) - this must
        never raise, since the result feeds stop-loss/position sizing.

        `_benchmark_returns` lets refresh_beta_registry() fetch the benchmark
        once and reuse it across an entire universe pass instead of re-fetching
        NIFTY_50 per symbol; leave it None for a one-off call on a single symbol.
        """
        fallback = config.Universe.BETA_REGISTRY.get(symbol, 1.0)

        # Fetch extra history so pct_change()'s dropped first row and any gaps
        # between the two series still leave a full lookback window after alignment.
        stock_df = self.fetch_daily_candles(symbol, days_back=lookback_days + 30)
        if stock_df.empty:
            logger.warning(f"No candle data for beta({symbol} vs {benchmark_symbol}); using fallback {fallback}.")
            return fallback

        if _benchmark_returns is not None:
            bench_ret = _benchmark_returns
        else:
            bench_df = self.fetch_daily_candles(benchmark_symbol, days_back=lookback_days + 30)
            if bench_df.empty:
                logger.warning(f"No candle data for beta({symbol} vs {benchmark_symbol}); using fallback {fallback}.")
                return fallback
            bench_ret = bench_df['close'].pct_change().dropna()

        stock_ret = stock_df['close'].pct_change().dropna()

        aligned = pd.concat(
            [stock_ret.rename('stock'), bench_ret.rename('bench')], axis=1, join='inner'
        ).tail(lookback_days)
        if len(aligned) < lookback_days * 0.5:
            logger.warning(
                f"Only {len(aligned)} aligned trading days for beta({symbol} vs {benchmark_symbol}); "
                f"using fallback {fallback}."
            )
            return fallback

        benchmark_variance = aligned['bench'].var()
        if not benchmark_variance or pd.isna(benchmark_variance):
            logger.warning(f"Zero/NaN benchmark variance computing beta({symbol}); using fallback {fallback}.")
            return fallback

        beta = aligned['stock'].cov(aligned['bench']) / benchmark_variance
        if pd.isna(beta):
            return fallback

        return max(0.5, min(2.5, float(beta)))

    def _read_live_beta_cache(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.live_beta_cache_path):
            return None
        try:
            with open(self.live_beta_cache_path, 'r') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not read '{self.live_beta_cache_path}': {e}")
            return None

    def _load_live_beta_overlay(self) -> None:
        """Applies today's persisted refresh_beta_registry() output (if any) to
        BETA_REGISTRY at startup. Missing file or a date other than today is a
        silent no-op - BETA_REGISTRY just keeps its static config.py values."""
        cached = self._read_live_beta_cache()
        if not cached:
            return
        if cached.get('date') != datetime.now(config.MARKET_TZ).date().isoformat():
            return
        values = cached.get('values')
        if not isinstance(values, dict):
            return
        config.Universe.BETA_REGISTRY.update(values)
        logger.info(f"Applied {len(values)} live beta values from today's refresh cache.")

    def refresh_beta_registry(
        self,
        symbols: Optional[List[str]] = None,
        benchmark_symbol: str = 'NIFTY_50',
        force: bool = False,
    ) -> int:
        """
        Recomputes live rolling betas and persists them to beta_registry_live.json,
        then applies them to config.Universe.BETA_REGISTRY for this process. This
        is the "wire live beta into the scanner" mechanism: scanner_engine.py is
        untouched and keeps reading BETA_REGISTRY exactly as before.

        Persistence matters because `main.py`'s CLI modes (btst/swing/discover/...)
        each run as a separate process - an in-memory-only update here would
        vanish the moment this process exits. Every DataBroker.__init__ loads and
        applies today's cache (see _load_live_beta_overlay), so running
        `refresh-beta` once reaches every later invocation that day without each
        of them paying the live-API cost themselves.

        Deliberately NOT called from every scan/CLI invocation - a full universe
        pass makes ~2x len(symbols) live API calls (one per symbol plus a shared
        benchmark fetch) at >=1.1s spacing, i.e. several minutes for the full
        TARGET_UNIVERSE. Gated to at most once per calendar day via the same
        cache file; pass force=True to override it. Symbols where the live fetch
        fails or is too short simply keep their existing BETA_REGISTRY value
        unchanged - that IS the static-fallback behavior, not a separate path.
        """
        today = datetime.now(config.MARKET_TZ).date().isoformat()
        if not force:
            cached = self._read_live_beta_cache()
            if cached and cached.get('date') == today:
                logger.info("BETA_REGISTRY already refreshed today; skipping (pass force=True to override).")
                return 0

        symbols = symbols or config.Universe.TARGET_UNIVERSE
        bench_df = self.fetch_daily_candles(benchmark_symbol, days_back=120)
        if bench_df.empty:
            logger.warning(f"Could not fetch '{benchmark_symbol}' candles; aborting BETA_REGISTRY refresh entirely.")
            return 0
        bench_ret = bench_df['close'].pct_change().dropna()

        live_values: Dict[str, float] = {}
        for symbol in symbols:
            live_values[symbol] = self.calculate_rolling_beta(
                symbol, benchmark_symbol=benchmark_symbol, _benchmark_returns=bench_ret
            )

        try:
            with open(self.live_beta_cache_path, 'w') as f:
                json.dump({'date': today, 'values': live_values}, f, indent=2, sort_keys=True)
        except OSError as e:
            logger.warning(f"Could not persist live beta cache to '{self.live_beta_cache_path}': {e}")

        config.Universe.BETA_REGISTRY.update(live_values)
        logger.info(f"Refreshed BETA_REGISTRY for {len(live_values)}/{len(symbols)} symbols ({today}).")
        return len(live_values)

    def get_live_candles(self, symbol: str, interval: str) -> pd.DataFrame:
        """
        Provides a combined view of historical and live, aggregated candles.
        This is the new primary way for the live scanner to get data.
        """
        # 1. Fetch historical data (e.g., last 10 days) for indicator calculation
        days_back = 10 if interval == 'FIFTEEN_MINUTE' else 5
        hist_df = self.fetch_ohlcv(symbol, interval, days_back, caller="LiveCandleBuilder")

        # 2. Get live aggregated candles from the WebSocket feed
        row = self._resolve_symbol_row(symbol)
        if row is None: return hist_df
        
        token = row['token']
        agg_interval = BROKER_TO_AGGREGATOR_INTERVAL.get(interval)
        if not agg_interval: return hist_df

        live_df = self.aggregator.get_candles(token, agg_interval)
        if live_df.empty: return hist_df

        # 3. Combine, de-duplicate, and return the unified series
        combined_df = pd.concat([hist_df, live_df], ignore_index=True).drop_duplicates(subset=['Timestamp'], keep='last').sort_values(by='Timestamp').reset_index(drop=True)
        return combined_df

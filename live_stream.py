import sys
import os
import time
import logging
import pandas as pd
import threading
from concurrent.futures import ThreadPoolExecutor
from data_broker import DataBroker
from scanner_engine import HybridScanner
import config
from orchestrator import execute_macro_discovery
from lifecycle_manager import shutdown_manager

try:
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
except ImportError:
    SmartWebSocketV2 = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class LiveVolumeScanner:
    def __init__(self, broker: DataBroker, scanner: HybridScanner, watchlist: list, discovery_df: pd.DataFrame, shutdown_event: threading.Event, strategy: str = 'SWING'):
        if SmartWebSocketV2 is None:
            raise ImportError("SmartWebSocketV2 is not installed. Please update your SmartApi python package.")
            
        self.broker = broker
        self.scanner = scanner
        self.watchlist = watchlist
        self.discovery_df = discovery_df
        self.strategy = strategy.upper()
        self.shutdown_event = shutdown_event
        self.token_to_symbol = {}
        self.sws = None
        self.scan_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix='LiveScannerWorker')
        self.last_scan_time = {} 
        
        self._prepare_tokens()
        
    def _prepare_tokens(self):
        self.broker._ensure_session()
        
        tokens = []
        for stock in self.watchlist:
            token_row = self.broker._resolve_symbol_row(stock)
            if not token_row.empty:
                token = str(token_row.iloc[0]['token'])
                tokens.append(token)
                self.token_to_symbol[token] = stock
                
        self.sws = SmartWebSocketV2(
            auth_token=self.broker.jwt_token,
            api_key=os.getenv('ANGEL_API_KEY'),
            client_code=os.getenv('ANGEL_CLIENT_CODE'),
            feed_token=self.broker.feed_token
        )
        
        self.sws.on_data = self.on_data
        self.sws.on_open = self.on_open
        self.sws.on_error = self.on_error
        self.sws.on_close = self.on_close
        
        self.tokens_to_subscribe = tokens
        
    def on_open(self, wsapp):
        if self.shutdown_event.is_set():
            return
        logger.info(f"✅ WebSocket connected! Subscribing to Live Stream in {self.strategy} Mode...")
        correlation_id = "volume_spike_scanner"
        mode = 3   
        
        token_list = [{"exchangeType": 1, "tokens": self.tokens_to_subscribe}]
        self.sws.subscribe(correlation_id, mode, token_list)
        
    def on_error(self, wsapp, error):
        if self.shutdown_event.is_set():
            return
        logger.error(f"WebSocket Error: {error}")
        
    def on_close(self, wsapp, *args):
        if self.shutdown_event.is_set():
            logger.info("🔌 WebSocket connection closed gracefully.")
        else:
            logger.warning("🔌 WebSocket connection closed unexpectedly by the server.")

    def on_data(self, wsapp, message):
        """Processes each incoming tick instantly."""
        if self.shutdown_event.is_set():
            return

        if 'token' in message and 'last_traded_quantity' in message:
            token = str(message['token'])
            symbol = self.token_to_symbol.get(token)
            if not symbol: return
            
            ltq = message.get('last_traded_quantity', 0)
            ltp = message.get('last_traded_price', 0) / 100.0 
            
            # Detect Volume Spike (A single block deal > 25,000 shares)
            if ltq >= config.Live.VOLUME_SPIKE_THRESHOLD_SHARES:
                self._handle_spike(symbol, ltq, ltp)

    def _handle_spike(self, symbol, ltq, ltp):
        """Debounces and offloads the heavy scan operation to a worker thread."""
        current_time = time.time()
        if symbol in self.last_scan_time and (current_time - self.last_scan_time[symbol]) < config.Live.DEBOUNCE_SECONDS:
            return
            
        self.last_scan_time[symbol] = current_time
        logger.warning(f"🚨 VOL SPIKE: {symbol} - Block of {ltq:,} shares traded at ₹{ltp:.2f}")
        
        if not self.shutdown_event.is_set():
            self.scan_executor.submit(self._trigger_instant_scan, symbol)
                
    def _trigger_instant_scan(self, symbol):
        """Executes a technical confirmation scan on the spiked symbol."""
        if self.shutdown_event.is_set():
            return

        logger.info(f"🔍 Running instant technical scan for {symbol} ({self.strategy})...")
        caller_context = "Live Stream Spike"
        
        s_row = self.discovery_df[self.discovery_df['Symbol'] == symbol].iloc[0]
        df_daily = s_row.get('_Daily_DF', pd.DataFrame())
        if df_daily.empty:
            df_daily = self.broker.fetch_ohlcv(symbol, 'ONE_DAY', 400, caller=caller_context)

        df_15min = self.broker.fetch_ohlcv(symbol, 'FIFTEEN_MINUTE', 5, caller=caller_context)
        
        nifty_df = self.broker.fetch_ohlcv('Nifty 50', 'ONE_DAY', 400, caller=caller_context)
        regime = self.scanner.compute_market_regime(nifty_df)
        
        if not df_daily.empty and not df_15min.empty:
            s_row = self.discovery_df[self.discovery_df['Symbol'] == symbol].iloc[0]
            
            signal = self.scanner.scan(
                symbol, df_daily, df_15min,
                rs_percentile=s_row['RS_Pctl'], sector_rs=s_row['Sector_RS'],
                regime_mult=regime['multiplier'], strategy=self.strategy
            )
            
            if signal:
                logger.info(f"✅ CONFIRMED SETUP: {symbol} | Score: {signal['Score']} | Target: {signal['Target']}")
                from orchestrator import SignalDB
                db = SignalDB()
                try:
                    db.log_signal(signal)
                finally:
                    db.close() # Ensure connection is closed.

    def start(self):
        """Starts the blocking WebSocket event loop with auto-reconnect."""
        logger.info(f"Starting Live Volume Streamer for {len(self.tokens_to_subscribe)} stocks...")
        
        while not self.shutdown_event.is_set():
            try:
                self.sws.connect()
            except Exception as e:
                if not self.shutdown_event.is_set():
                    logger.error(f"WebSocket crashed: {e}")
            
            if not self.shutdown_event.is_set():
                logger.warning("Connection lost. Auto-reconnecting in 5 seconds...")
                time.sleep(5)
                self._prepare_tokens()

    def stop(self):
        """Stops the WebSocket and shuts down worker threads."""
        print("Stopping live scanner...")
        logger.info("Stopping live scanner...")

        print("Stopping websocket...")
        logger.info("Stopping websocket...")
        if self.sws:
            self.sws.close_connection()

        print("Stopping worker threads...")
        logger.info("Stopping worker threads...")
        self.scan_executor.shutdown(wait=True, cancel_futures=True)

if __name__ == "__main__":
    strategy = 'SWING'
    force_refresh = "--force-refresh" in sys.argv[1:]
    positional_args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if positional_args:
        arg = positional_args[0].strip().lower()
        if arg in ['btst', 'swing', 'gap']:
            strategy = arg.upper()
        else:
            print(f"⚠️ Unknown strategy '{arg}'. Supported strategies: swing, btst, gap. Defaulting to SWING.")

    broker = DataBroker()
    scanner = HybridScanner()
    
    logger.info(f"Initializing Live System. Running Pre-Market Discovery for {strategy}...")
    watchlist, full_df = execute_macro_discovery(broker, scanner, strategy=strategy, force_refresh=force_refresh, caller="Live Stream Discovery")
    
    if watchlist:
        streamer = LiveVolumeScanner(broker, scanner, watchlist, full_df, shutdown_manager.shutdown_event, strategy=strategy)
        shutdown_manager.register(streamer.stop)
        
        streamer_thread = threading.Thread(target=streamer.start, name="LiveStreamerThread")
        streamer_thread.daemon = True
        streamer_thread.start()

        try:
            # Keep the main thread alive to wait for shutdown signal
            shutdown_manager.shutdown_event.wait()
        except KeyboardInterrupt:
            # This is redundant as the signal handler in LifecycleManager handles it,
            # but it's good practice for clarity.
            pass
        finally:
            if not shutdown_manager.is_shutdown():
                shutdown_manager.initiate_shutdown()
            # Wait for the streamer thread to finish
            streamer_thread.join(timeout=10)

    else:
        logger.warning("No candidates found during discovery. WebSocket aborted.")

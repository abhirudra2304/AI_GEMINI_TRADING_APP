import os
import pandas as pd
from datetime import datetime
import logging
from data_broker import DataBroker
from scanner_engine import HybridScanner
from orchestrator import (
    SignalDB,
    DiscoveryCache,
    execute_macro_discovery,
    run_manual_scan,
    MARKET_TZ,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_historical_generation(days_back=30, strategy: str = 'BTST'):
    """
    Simulates EOD market conditions over a historical window and logs triggered
    signals into the local database for backtesting. This runs the full
    discovery and confirmation scan for each day to generate realistic signals.
    """
    logger.info(f"🕰️  Initiating Historical Generator: Simulating EOD {strategy} scans for the past {days_back} days...")
    
    broker = DataBroker()
    scanner = HybridScanner()
    db = SignalDB()
    
    # Fetch a long history for Nifty to establish trading dates and allow for indicator lookbacks.
    nifty_full_df = broker.fetch_ohlcv('Nifty 50', 'ONE_DAY', days_back + 250, caller="HistGen-Nifty")
    
    if nifty_full_df.empty:
        logger.error("Failed to fetch Nifty data. Cannot determine historical trading dates.")
        return
        
    nifty_full_df['Date'] = pd.to_datetime(nifty_full_df['Timestamp']).dt.date
    # Get the last `days_back` trading dates from the available data.
    trading_dates = sorted(nifty_full_df['Date'].unique())[-days_back:]
    
    signals_generated = 0

    for target_date in trading_dates:
        logger.info(f"\n📅 Processing Date: {target_date}")
        
        # Create a point-in-time datetime object for the end of the historical day.
        point_in_time = datetime.combine(target_date, datetime.max.time()).replace(tzinfo=MARKET_TZ)

        # Use a temporary, in-memory cache for this run to avoid polluting the main cache.
        temp_cache = DiscoveryCache(path=None)
        
        # 1. Run discovery for this historical date.
        watchlist, discovered_df = execute_macro_discovery(
            broker, scanner, strategy=strategy, force_refresh=True, cache=temp_cache, point_in_time=point_in_time, caller="HistGen-Discovery"
        )
        
        if watchlist:
            # 2. Run confirmation scan for the same historical date.
            df_signals = run_manual_scan(
                broker, scanner, watchlist, discovered_df, strategy=strategy, persist=False, display=False, force_refresh=True, point_in_time=point_in_time, caller="HistGen-Scan"
            )
            
            if not df_signals.empty:
                # 3. Log generated signals to the database with the correct historical timestamp.
                for _, sig_row in df_signals.iterrows():
                    sig = sig_row.to_dict()
                    # Override the 'now' timestamp with the historical EOD timestamp.
                    sig['timestamp'] = point_in_time.isoformat(timespec='seconds')
                    db.log_signal(sig)
                    signals_generated += 1
                    logger.info(f"✅ Ghost Signal Logged: [{sig['Symbol']}] on {target_date} (Score: {sig['Score']})")

    logger.info("=" * 60)
    logger.info(f"🏁 Historical Generation Complete. Logged {signals_generated} {strategy} setups to signals.db.")
    logger.info("You may now run `python backtest.py` to evaluate strategy performance.")
    logger.info("=" * 60)

if __name__ == "__main__":
    # Example: python historical_generator.py 90 BTST
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    strat = sys.argv[2].upper() if len(sys.argv) > 2 else 'BTST'
    run_historical_generation(days_back=days, strategy=strat)
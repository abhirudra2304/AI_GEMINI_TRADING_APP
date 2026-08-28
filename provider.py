import os
import logging
from datetime import datetime
from typing import List, Optional, Dict

import pandas as pd

import config

logger = logging.getLogger(__name__)

class ConstituentProvider:
    """
    Provides a point-in-time stock universe to mitigate survivorship bias.

    This class loads a historical record of index constituents (e.g., Nifty 500)
    and returns the correct list of stocks that were part of the index on a
    given date. For live runs (when no date is provided), it falls back to the
    manually curated list in `config.py`.
    """

    def __init__(self, index_name: str = "nifty500"):
        self.index_name = index_name.lower().replace(" ", "")
        self.filepath = f"data/{self.index_name}_constituents.csv"
        self.constituents_df: Optional[pd.DataFrame] = None
        self.default_universe: List[str] = config.Universe.TARGET_UNIVERSE
        self._load_data()

    def _load_data(self):
        """
        Loads the historical constituent data from a CSV file.
        The CSV must have 'date' and 'symbol' columns.
        """
        if not os.path.exists(self.filepath):
            logger.warning(
                f"Historical constituent file not found at '{self.filepath}'. "
                "Backtester will have survivorship bias. Live scanner will use config universe."
            )
            return

        try:
            df = pd.read_csv(self.filepath, parse_dates=['date'])
            if 'date' not in df.columns or 'symbol' not in df.columns:
                logger.error(
                    f"'{self.filepath}' must contain 'date' and 'symbol' columns."
                )
                return
            
            # Sort by date to allow efficient lookups
            df = df.sort_values('date').reset_index(drop=True)
            self.constituents_df = df
            logger.info(f"✅ Successfully loaded {len(df)} historical constituent records from '{self.filepath}'.")

        except Exception as e:
            logger.error(f"Failed to load or parse historical constituents from '{self.filepath}': {e}")

    def get_universe(self, point_in_time: Optional[datetime] = None) -> List[Dict[str, str]]:
        """
        Gets the stock universe for a specific point in time.

        Args:
            point_in_time: The date for which to get the universe. If None,
                           returns the default universe from config.py for live runs.

        Returns:
            A list of dictionaries, each containing 'symbol' and 'sector'.
        """
        symbols: List[str]
        if point_in_time is None:
            logger.info(f"Using default live universe with {len(self.default_universe)} stocks.")
            symbols = self.default_universe
        elif self.constituents_df is None or self.constituents_df.empty:
            logger.warning(
                f"No historical constituent data available. Falling back to default universe for date {point_in_time.date()}. "
                "WARNING: This introduces survivorship bias into the backtest."
            )
            symbols = self.default_universe
        else:
            target_date = point_in_time.date()

            # Find the most recent date in the CSV that is less than or equal to the target date
            # `searchsorted` gives the index where the element should be inserted to maintain order.
            # The index before that is the last date that is <= our target date.
            idx = self.constituents_df['date'].searchsorted(pd.Timestamp(target_date), side='right') - 1

            if idx < 0:
                logger.warning(f"No constituent data found on or before {target_date}. Using earliest available data.")
                effective_date = self.constituents_df['date'].iloc[0]
            else:
                effective_date = self.constituents_df['date'].iloc[idx]
            
            # Get all symbols for that effective date
            symbols = self.constituents_df[self.constituents_df['date'] == effective_date]['symbol'].unique().tolist()
            logger.info(f"Using historical universe of {len(symbols)} stocks for date {target_date} (data from {effective_date.date()}).")

        # Now, build the list of dictionaries with sector info
        universe_with_sectors = []
        for symbol in symbols:
            sector = config.Universe.SECTOR_MAP.get(symbol, 'OTHER')
            universe_with_sectors.append({'symbol': symbol, 'sector': sector})
            
        return universe_with_sectors

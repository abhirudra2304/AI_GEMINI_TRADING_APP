import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

class EarningsAnalyzer:
    def __init__(self, danger_zone_days=5, new_listing_history_days=65):
        """
        :param danger_zone_days: Number of days before earnings to flag as HIGH RISK.
        :param new_listing_history_days: Minimum daily trading sessions a ticker
            must have before an UNKNOWN earnings date is treated as an accepted
            data-source gap (see evaluate_risk) rather than a hard veto. 65
            sessions is roughly one full earnings cycle plus a buffer - below
            that, there's no trading track record to fall back on, so an
            unconfirmed earnings date can't be waved through as "probably fine".
        """
        self.danger_zone_days = danger_zone_days
        self.new_listing_history_days = new_listing_history_days
        self.earnings_cache = {}  # Cache to store dates and prevent API spam

    def _fetch_earnings_date(self, symbol):
        """Fetches the next earnings date using Yahoo Finance."""
        # Standardize for NSE
        yf_symbol = f"{symbol}.NS"
        
        try:
            ticker = yf.Ticker(yf_symbol)
            calendar = ticker.calendar
            
            # yfinance API update fix: calendar can be a dict or a DataFrame
            # We use len() instead of .empty to safely check both types
            if calendar is not None and len(calendar) > 0:
                if 'Earnings Date' in calendar:
                    earnings_dates = calendar['Earnings Date']
                    
                    # Handle both list (from dict) and Series (from DataFrame)
                    if len(earnings_dates) > 0:
                        # pd.to_datetime safely parses both formats
                        return pd.to_datetime(earnings_dates[0]).date()
            return None
        except Exception as e:
            print(f"⚠️ Earnings API Error for {symbol}: {str(e)}")
            return None

    def evaluate_risk(self, symbol, history_days=None):
        """
        Evaluates if the stock is too close to an earnings announcement.
        Returns a dictionary with the risk status.

        :param history_days: Trading sessions of daily price history available
            for this ticker, if known. None (unknown/unfetchable) is treated
            the same as "too little history" - a ticker whose price data
            couldn't even be fetched must not get *less* scrutiny than one
            that fetched cleanly but is simply new.
        """
        today = datetime.now().date()
        
        # Check cache first. A None result (data source had nothing for this
        # symbol, or a transient fetch failure) is deliberately NOT cached -
        # caching it would silently and permanently suppress the veto for that
        # symbol for this scanner instance's whole lifetime, indistinguishable
        # from a genuine "no earnings scheduled" result.
        if symbol in self.earnings_cache:
            next_earnings = self.earnings_cache[symbol]
        else:
            next_earnings = self._fetch_earnings_date(symbol)
            if next_earnings is not None:
                self.earnings_cache[symbol] = next_earnings

        if next_earnings is None:
            # Yahoo Finance's earnings calendar has no date for some NSE stocks
            # (thin analyst coverage, e.g. CYIENTDLM) even when the company has a
            # real, scheduled board meeting - this is a data-source gap, not "no
            # event risk". UNKNOWN must never look like a cleared/safe result to
            # callers, so it's surfaced as its own risk tier here rather than
            # silently passing signals through unflagged.
            #
            # That soft flag alone is a blind spot for genuinely new/recently
            # listed tickers, though: an established stock with thin analyst
            # coverage (CYIENTDLM-class) has a long, clean trading history to
            # fall back on even without a confirmed earnings date, but a brand
            # new listing has no track record at all - "probably no real event"
            # isn't a safe assumption there. Hard-veto instead when history is
            # too short (or unknown/unfetchable) to have earned that benefit of
            # the doubt.
            if history_days is None or history_days < self.new_listing_history_days:
                return {
                    "Earnings_Date": "Unknown",
                    "Days_To_Earnings": 0,
                    "Earnings_Risk": "HIGH RISK 🛑 (New/Recent Listing - No Earnings History To Verify Safety)"
                }
            return {
                "Earnings_Date": "Unknown",
                "Days_To_Earnings": 999,
                "Earnings_Risk": "UNKNOWN ❓ (No data - verify manually before entry)"
            }
            
        # Calculate days until earnings
        # If the date is in the past, it means Yahoo hasn't updated the next quarter yet
        if next_earnings < today:
            return {
                "Earnings_Date": str(next_earnings),
                "Days_To_Earnings": 999,
                "Earnings_Risk": "CLEARED (Already Reported)"
            }
            
        days_to_earnings = (next_earnings - today).days
        
        # Determine Risk Level
        if days_to_earnings <= self.danger_zone_days:
            risk_level = "HIGH RISK 🛑 (Imminent Earnings)"
        elif days_to_earnings <= 15:
            risk_level = "ELEVATED ⚠️ (Earnings Approaching)"
        else:
            risk_level = "SAFE ✅"
            
        return {
            "Earnings_Date": str(next_earnings),
            "Days_To_Earnings": days_to_earnings,
            "Earnings_Risk": risk_level
        }
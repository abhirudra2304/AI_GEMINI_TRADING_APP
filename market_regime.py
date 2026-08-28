"""
Market Regime Intelligence Module

This module analyzes broad market health to produce a score, state, and
actionable recommendation for trading. It is designed to be a standalone
observer and does not interfere with individual stock scanning logic.
"""
import pandas as pd
from typing import Dict, List, Any, Tuple
from data_broker import DataBroker
from scanner_engine import HybridScanner

class MarketRegime:
    def __init__(self, broker: DataBroker, scanner: HybridScanner, universe: List[str], discovery_df: pd.DataFrame = None):
        self.broker = broker
        self.scanner = scanner
        self.universe = universe
        self.discovery_df = discovery_df
        self.indices = ['Nifty 50', 'NIFTY BANK', 'NIFTY MIDCAP 100', 'NIFTY SMALLCAP 100']
        self.vix_symbol = 'India VIX'
        self.data = {}
        self.results = {}

    def _fetch_data(self):
        """Fetches only the essential data for indices and VIX."""
        for index in self.indices:
            self.data[index] = self.broker.fetch_ohlcv(index, 'ONE_DAY', 200, caller="MarketRegime")
        
        self.data[self.vix_symbol] = self.broker.fetch_ohlcv(self.vix_symbol, 'ONE_DAY', 50, caller="MarketRegime")

    def _calculate_trends(self) -> Dict[str, Any]:
        """Calculates the trend for major indices."""
        trends = {}
        for index in self.indices:
            df = self.data.get(index)
            if df is not None and not df.empty and len(df) > 50:
                df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
                df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
                latest_close = df['Close'].iloc[-1]
                ema20 = df['EMA20'].iloc[-1]
                ema50 = df['EMA50'].iloc[-1]
                
                state = "BULL" if latest_close > ema50 else "BEAR"
                strength = "STRONG" if latest_close > ema20 else "WEAK"
                trends[index] = {'state': state, 'strength': strength}
            else:
                trends[index] = {'state': 'UNKNOWN', 'strength': 'UNKNOWN'}
        return trends

    def _calculate_vix_trend(self) -> Dict[str, Any]:
        """Analyzes the India VIX for market fear."""
        df = self.data.get(self.vix_symbol)
        if df is not None and not df.empty:
            latest_vix = df['Close'].iloc[-1]
            if latest_vix < 15:
                return {'level': 'LOW', 'implication': 'Complacency/Confidence'}
            elif latest_vix < 25:
                return {'level': 'NEUTRAL', 'implication': 'Normal Volatility'}
            else:
                return {'level': 'HIGH', 'implication': 'Fear/Uncertainty'}
        return {'level': 'UNKNOWN', 'implication': 'UNKNOWN'}

    def _calculate_market_timing(self) -> Dict[str, Any]:
        """Calculates Distribution Days and Follow Through Days."""
        df = self.data.get('Nifty 50')
        if df is None or df.empty or len(df) < 25:
            return {'distribution_days': -1, 'follow_through_day': False}

        df['change'] = df['Close'].pct_change() * 100
        df['volume_change'] = df['Volume'].pct_change()

        distribution_days = 0
        for i in range(len(df) - 25, len(df)):
            if df['change'].iloc[i] <= -0.2 and df['volume_change'].iloc[i] > 0:
                distribution_days += 1
        
        ftd_active = False
        rally_day_count = 0
        # Simplified FTD logic - requires state machine for full accuracy
        for i in range(len(df) - 15, len(df)):
            if df['change'].iloc[i] > 0:
                rally_day_count += 1
            else:
                rally_day_count = 0
            
            if 4 <= rally_day_count <= 10:
                if df['change'].iloc[i] > 1.5 and df['volume_change'].iloc[i] > 0:
                    ftd_active = True
                    break

        return {'distribution_days': distribution_days, 'follow_through_day': ftd_active}

    def _calculate_market_breadth(self) -> Dict[str, Any]:
        """Calculates market breadth using percentage of stocks above 50-DMA."""
        if self.discovery_df is None or self.discovery_df.empty or '_Daily_Metrics' not in self.discovery_df.columns:
            return {'stocks_above_50dma_pct': -1}

        above_50dma = 0
        total_stocks = 0

        for metrics in self.discovery_df['_Daily_Metrics']:
            if not isinstance(metrics, dict):
                continue
            
            close = metrics.get('Close')
            ema50 = metrics.get('EMA50')

            if close is not None and ema50 is not None and not pd.isna(close) and not pd.isna(ema50):
                total_stocks += 1
                if close > ema50:
                    above_50dma += 1
        
        if total_stocks == 0:
            return {'stocks_above_50dma_pct': 0}
            
        pct_above = (above_50dma / total_stocks) * 100
        return {'stocks_above_50dma_pct': pct_above}

    def _calculate_sector_leadership(self) -> pd.DataFrame:
        """Identifies leading and lagging sectors based on average RS."""
        if self.discovery_df is None or self.discovery_df.empty or 'Sector' not in self.discovery_df.columns or 'RS_Pctl' not in self.discovery_df.columns:
            return pd.DataFrame()
        
        sector_rs = self.discovery_df.groupby('Sector')['RS_Pctl'].mean().sort_values(ascending=False)
        return sector_rs.reset_index()

    def calculate_regime(self) -> Dict[str, Any]:
        """
        Orchestrates all calculations and computes the final market score and state.
        """
        self._fetch_data()
        
        # Perform all calculations
        self.results['trends'] = self._calculate_trends()
        self.results['vix'] = self._calculate_vix_trend()
        self.results['timing'] = self._calculate_market_timing()
        self.results['breadth'] = self._calculate_market_breadth()
        self.results['sector_leadership'] = self._calculate_sector_leadership()
        
        score = 0
        # 1. Nifty Trend (25 points)
        if self.results['trends']['Nifty 50']['state'] == 'BULL':
            score += 15
            if self.results['trends']['Nifty 50']['strength'] == 'STRONG':
                score += 10
        
        # 2. Broader Market Trend (20 points)
        if self.results['trends']['NIFTY MIDCAP 100']['state'] == 'BULL':
            score += 10
        if self.results['trends']['NIFTY SMALLCAP 100']['state'] == 'BULL':
            score += 10

        # 3. Market Timing Signals (25 points)
        dd = self.results['timing']['distribution_days']
        if dd <= 2:
            score += 15
        elif dd <= 4:
            score += 10
        elif dd <= 6:
            score += 5
        if self.results['timing']['follow_through_day']:
            score += 10

        # 4. Market Breadth (20 points)
        breadth_pct = self.results['breadth'].get('stocks_above_50dma_pct', 0)
        if breadth_pct > 70:
            score += 20
        elif breadth_pct > 50:
            score += 15
        elif breadth_pct > 30:
            score += 10

        # 5. VIX (10 points)
        if self.results['vix']['level'] == 'LOW':
            score += 10
        elif self.results['vix']['level'] == 'NEUTRAL':
            score += 5
            
        self.results['market_score'] = min(int(score), 100)

        if score >= 75:
            state, reco = 'STRONG BULL', 'Full Position'
        elif score >= 60:
            state, reco = 'BULL', 'Full Position'
        elif score >= 40:
            state, reco = 'NEUTRAL', 'Half Position'
        elif score >= 20:
            state, reco = 'WEAK', 'Wait'
        else:
            state, reco = 'BEAR', 'No BTST / Contra only'
            
        self.results['market_state'] = state
        self.results['recommendation'] = reco
        
        return self.results
    
    def print_report(self):
        if not self.results:
            print("Market Regime report has not been generated yet.")
            return
            
        print("\n" + "="*52)
        print("MARKET REGIME INTELLIGENCE")
        print("="*52)
        print(f"MARKET SCORE         : {self.results['market_score']}/100")
        print(f"MARKET STATE         : {self.results['market_state']}")
        print(f"RECOMMENDATION       : {self.results['recommendation']}")
        print("-" * 52)
        print(f"NIFTY 50 Trend       : {self.results['trends']['Nifty 50']['state']} ({self.results['trends']['Nifty 50']['strength']})")
        print(f"BANKNIFTY Trend      : {self.results['trends']['NIFTY BANK']['state']}")
        print(f"MIDCAP Trend         : {self.results['trends']['NIFTY MIDCAP 100']['state']}")
        print(f"SMALLCAP Trend       : {self.results['trends']['NIFTY SMALLCAP 100']['state']}")
        print("-" * 52)
        print(f"India VIX Level      : {self.results['vix']['level']} ({self.results['vix']['implication']})")
        print(f"Distribution Days    : {self.results['timing']['distribution_days']} (in last 25 sessions)")
        print(f"Follow Through Day   : {'ACTIVE' if self.results['timing']['follow_through_day'] else 'INACTIVE'}")
        print(f"Market Breadth       : {self.results['breadth'].get('stocks_above_50dma_pct', -1):.1f}% stocks > 50-DMA")
        print("-" * 52)
        
        sector_df = self.results.get('sector_leadership')
        if sector_df is not None and not sector_df.empty:
            print("SECTOR LEADERSHIP (Top 5)")
            print(sector_df.head(5).to_string(index=False))
        
        print("="*52)

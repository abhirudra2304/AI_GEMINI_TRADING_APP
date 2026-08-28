import numpy as np
import pandas as pd
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo
import config
from silent_accumulation import compute_silent_metrics

logger = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("Asia/Kolkata")

class HybridScanner:
    SECTOR_MAP = config.Universe.SECTOR_MAP

    def __init__(self, base_multiplier: float = 2.0, alpha: float = 0.5, 
                 max_risk_per_trade: float = 5000, max_capital_per_trade: float = 100000):
        self.base_multiplier = base_multiplier
        self.alpha = alpha
        self.max_risk_per_trade = max_risk_per_trade 
        self.max_capital_per_trade = max_capital_per_trade
        self.beta_registry = config.Universe.BETA_REGISTRY

    def compute_market_regime(self, nifty_df: pd.DataFrame) -> Dict[str, Any]:
        """Analyzes NIFTY data to determine market conditions and multipliers."""
        if len(nifty_df) < 200: return {'multiplier': 1.0, 'block': False, 'label': 'INSUFFICIENT_DATA'}
        
        close = nifty_df['Close'].iloc[-1]
        ema20 = nifty_df['Close'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = nifty_df['Close'].ewm(span=50, adjust=False).mean().iloc[-1]
        ema200 = nifty_df['Close'].ewm(span=200, adjust=False).mean().iloc[-1]
        
        is_short_term_bullish = (close > ema20) or (nifty_df['Close'].iloc[-1] > nifty_df['Close'].iloc[-2] > nifty_df['Close'].iloc[-3])
        
        if close < ema200: 
            if is_short_term_bullish:
                return {'multiplier': 0.7, 'block': False, 'label': 'BEARISH_RECOVERY'}
            return {'multiplier': 0.5, 'block': False, 'label': 'EXTREME_BEAR'} 
            
        if close > ema20 > ema50 > ema200: return {'multiplier': 1.0, 'block': False, 'label': 'BULLISH'}
        if close > ema50: return {'multiplier': 0.9, 'block': False, 'label': 'NEUTRAL'}
        return {'multiplier': 0.7, 'block': False, 'label': 'BEARISH'}

    def _completed_daily_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        """Returns sorted daily frame. Intentionally keeps today's candle for live volume shock detection."""
        if df.empty or 'Timestamp' not in df.columns:
            return df
        daily = df.copy()
        daily['Timestamp'] = pd.to_datetime(daily['Timestamp'], errors='coerce')
        daily = daily.dropna(subset=['Timestamp']).sort_values('Timestamp')
        return daily

    def compute_daily_metrics(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """Calculates all indicators required for scanning and scoring."""
        df = self._completed_daily_frame(df)
        if len(df) < 50: return None
        df = df.copy()
        
        # Calculate moving averages
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
        df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()

        # Calculate Pivots
        df['Pivot_50'] = df['High'].shift(1).rolling(window=50).max()
        df['Pivot_250'] = df['High'].shift(1).rolling(window=250).max()
        
        # Calculate Liquidity
        df['Traded_Value'] = df['Volume'] * df['Close']
        df['Avg_Traded_Value_20d'] = df['Traded_Value'].rolling(20).mean()
        
        # Calculate ATR
        high_low = df['High'] - df['Low']
        high_close = np.abs(df['High'] - df['Close'].shift())
        low_close = np.abs(df['Low'] - df['Close'].shift())
        df['atr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).ewm(span=14, adjust=False).mean()
        
        # Calculate ADX
        df['up_move'] = df['High'] - df['High'].shift(1)
        df['down_move'] = df['Low'].shift(1) - df['Low']
        df['p_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
        df['n_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
        p_di = 100 * (df['p_dm'].ewm(span=14, adjust=False).mean() / df['atr'])
        n_di = 100 * (df['n_dm'].ewm(span=14, adjust=False).mean() / df['atr'])
        df['adx'] = (100 * np.abs(p_di - n_di) / (p_di + n_di + 1e-10)).ewm(span=14, adjust=False).mean()
        
        # Calculate RSI
        delta = df['Close'].diff()
        gain = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        metrics = df.iloc[-1].to_dict()

        # Silent accumulation footprint (see silent_accumulation.py). Computed
        # once here so both the discovery ranking and the confirmation scan can
        # read it without recomputing indicators.
        silent_metrics = compute_silent_metrics(df)
        if silent_metrics:
            metrics.update(silent_metrics)

        return metrics

    def _prepare_intraday_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        intraday = df.copy()
        if 'Timestamp' in intraday.columns:
            intraday['Timestamp'] = pd.to_datetime(intraday['Timestamp'], errors='coerce')
            intraday = intraday.dropna(subset=['Timestamp']).sort_values('Timestamp')
        return intraday.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])

    def _intraday_rsi(self, close: pd.Series, period: int = 14) -> float:
        if close.empty:
            return 50.0
        delta = close.diff()
        gain = delta.where(delta > 0, 0).ewm(alpha=1/period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        flat = (gain == 0) & (loss == 0)
        rsi = rsi.where(loss != 0, 100)
        rsi = rsi.where(gain != 0, 0)
        rsi = rsi.where(~flat, 50)
        latest = rsi.iloc[-1]
        return 50.0 if pd.isna(latest) else float(latest)

    def _latest_supertrend(self, df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> float:
        """Calculates the latest Supertrend value using the pandas-ta library."""
        if len(df) < period:
            return np.nan
        try:
            import pandas_ta as ta
        except ImportError:
            logger.warning("pandas_ta not installed. Supertrend will be ignored. Run: pip install pandas_ta")
            return np.nan

        # pandas-ta expects lowercase column names
        df_ta = df.copy()
        df_ta.columns = [col.lower() for col in df_ta.columns]

        st = ta.supertrend(df_ta['high'], df_ta['low'], df_ta['close'], length=period, multiplier=multiplier)

        if st is None or st.empty:
            return np.nan

        # The supertrend line is the first column of the result, e.g., 'SUPERT_10_3.0'
        supertrend_values = st.iloc[:, 0]
        latest_value = supertrend_values.iloc[-1]

        return float(latest_value) if pd.notna(latest_value) else np.nan

    def compute_intraday_execution_score(self, df_5min: pd.DataFrame, df_15min: pd.DataFrame) -> Dict[str, Any]:
        fast = self._prepare_intraday_frame(df_5min)
        slow = self._prepare_intraday_frame(df_15min)
        primary = fast if len(fast) >= 20 else slow
        trend_frame = slow if len(slow) >= 20 else primary

        if primary.empty:
            return {
                'Execution_Score': 0,
                'Execution_Grade': 'D',
                'VWAP': 'N/A',
                'Above_VWAP': 'NO',
                'Intraday_Trend': 'Weak',
                'Closing_Strength': 'Weak',
                'Execution_Recommendation': 'WAIT',
                '_Execution_RSI': 50.0,
                '_VWAP_Distance': 0.0,
                '_Afternoon_Momentum': False,
            }

        latest = primary.iloc[-1]
        price = float(latest['Close'])
        typical_price = (primary['High'] + primary['Low'] + primary['Close']) / 3
        cumulative_volume = primary['Volume'].replace(0, np.nan).cumsum()
        vwap_series = (typical_price * primary['Volume']).cumsum() / cumulative_volume
        vwap = float(vwap_series.iloc[-1]) if not pd.isna(vwap_series.iloc[-1]) else price
        above_vwap = price > vwap
        vwap_distance = ((price - vwap) / vwap) * 100 if vwap > 0 else 0.0

        supertrend = self._latest_supertrend(trend_frame)
        above_supertrend = not pd.isna(supertrend) and price > supertrend

        ema5 = primary['Close'].ewm(span=5, adjust=False).mean().iloc[-1]
        ema20 = primary['Close'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = primary['Close'].ewm(span=50, adjust=False).mean().iloc[-1] if len(primary) >= 50 else np.nan
        ema_aligned = not pd.isna(ema50) and ema5 > ema20 and ema20 > ema50

        intraday_rsi = self._intraday_rsi(primary['Close'])
        current_volume = float(primary['Volume'].iloc[-1])
        avg_volume_20 = float(primary['Volume'].tail(21).iloc[:-1].mean()) if len(primary) > 20 else float(primary['Volume'].mean())
        volume_ratio = current_volume / avg_volume_20 if avg_volume_20 > 0 else 0.0

        intraday_high = float(primary['High'].max())
        high_distance = ((intraday_high - price) / intraday_high) * 100 if intraday_high > 0 else 100.0

        afternoon_momentum = False
        if 'Timestamp' in primary.columns:
            timestamps = primary['Timestamp'].dt.tz_localize(None)
            afternoon = primary[timestamps.dt.time >= datetime.strptime("14:30", "%H:%M").time()]
            if len(afternoon) >= 3:
                afternoon_momentum = bool(
                    afternoon['High'].iloc[-1] > afternoon['High'].iloc[0]
                    and afternoon['Low'].iloc[-1] > afternoon['Low'].iloc[0]
                    and afternoon['Close'].iloc[-1] > afternoon['Open'].iloc[-1]
                )

        score = 0
        score += 10 if above_vwap else 0
        score += 10 if above_supertrend else 0
        score += 10 if ema_aligned else 0
        if 55 <= intraday_rsi <= 70:
            score += 10
        elif 50 <= intraday_rsi < 55:
            score += 5
        elif intraday_rsi > 75:
            score += 3
        if volume_ratio > 1.5:
            score += 10
        elif volume_ratio > 1.2:
            score += 5
        if high_distance < 0.5:
            score += 10
        elif high_distance <= 1.0:
            score += 5
        if 0 <= vwap_distance <= 1:
            score += 5
        elif vwap_distance < 0:
            score += 0
        score += 10 if afternoon_momentum else 0

        if score >= 65:
            grade = 'A+'
        elif score >= 55:
            grade = 'A'
        elif score >= 42:
            grade = 'B'
        elif score >= 30:
            grade = 'C'
        else:
            grade = 'D'

        momentum = 'Strong' if afternoon_momentum and above_vwap and ema_aligned else ('Constructive' if score >= 42 else 'Weak')
        closing_strength = 'Strong' if high_distance < 0.5 else ('Acceptable' if high_distance <= 1.0 else 'Weak')
        recommendation = 'BUY TODAY' if score >= 55 and above_vwap and closing_strength != 'Weak' else ('WATCH' if score >= 42 else 'WAIT')

        return {
            'Execution_Score': int(score),
            'Execution_Grade': grade,
            'VWAP': round(vwap, 2),
            'Above_VWAP': 'YES' if above_vwap else 'NO',
            'Intraday_Trend': momentum,
            'Closing_Strength': closing_strength,
            'Execution_Recommendation': recommendation,
            '_Execution_RSI': round(intraday_rsi, 1),
            '_VWAP_Distance': round(vwap_distance, 2),
            '_Afternoon_Momentum': afternoon_momentum,
        }

    def _execution_rationale(self, base_summary: str, execution: Dict[str, Any], breakout_bonus: int) -> str:
        if not execution:
            return base_summary
        score = execution.get('Execution_Score', 0)
        above_vwap = execution.get('Above_VWAP') == 'YES'
        momentum = execution.get('Intraday_Trend', 'Weak')
        recommendation = execution.get('Execution_Recommendation', 'WAIT')
        if score >= 55 and above_vwap and momentum in ['Strong', 'Constructive']:
            prefix = "Daily breakout confirmed" if breakout_bonus > 0 else "Excellent daily trend confirmed"
            return f"{prefix} with rising intraday momentum and price holding above VWAP into the close. Execution: {recommendation}."
        if above_vwap and score >= 42:
            return f"Strong daily setup with price above VWAP, but execution quality is only {execution.get('Execution_Grade')}. Watch for stronger afternoon follow-through."
        return "Excellent daily trend but weak afternoon momentum. Wait for better execution."

    def scan_silent(self, symbol: str, df_daily: pd.DataFrame, daily_metrics: Optional[Dict[str, Any]] = None,
                    rs_percentile: float = 50.0, sector_rs: float = 50.0,
                    regime_mult: float = 1.0) -> Optional[Dict[str, Any]]:
        """
        Scores a stock on the silent-accumulation footprint instead of momentum.

        The momentum path pays for volume shocks, ADX velocity and 250-day
        breakouts - exactly the loud behaviour a stealth institutional bid
        avoids. Here the reward goes to persistence of trend: a smooth
        regression slope, a compressed ATR and price riding the 20 EMA with no
        single flashy day. Sizing is still ATR-based, but with a wider
        multiplier because a quiet stock's ATR is small and a tight stop would
        be noise-triggered.
        """
        cfg = config.Silent
        d_metrics = daily_metrics if daily_metrics is not None else self.compute_daily_metrics(df_daily)
        if d_metrics is None:
            return None

        silent_score = d_metrics.get('Silent_Score')
        if silent_score is None or pd.isna(silent_score):
            return None

        cp = d_metrics.get('Close', 0)
        ema50 = d_metrics.get('EMA50', cp)
        ema20 = d_metrics.get('EMA20', cp)
        atr_val = d_metrics.get('atr', cp * 0.02)
        rsi_val = d_metrics.get('RSI', 50)
        adx_val = d_metrics.get('adx', 0)
        pivot_50 = d_metrics.get('Pivot_50', cp)
        pivot_250 = d_metrics.get('Pivot_250', cp)

        liquidity_val = d_metrics.get('Avg_Traded_Value_20d', 0)
        liquidity_label = "HIGH" if liquidity_val > 100_000_000 else "LOW"

        # Hard filter. The RSI and RS floors are softer than the momentum path:
        # a stock grinding up 1% a day rarely prints an overbought RSI, and it
        # is usually still climbing the RS ladder rather than topping it.
        if (liquidity_label == "LOW" or cp < ema50
                or rsi_val < cfg.RSI_THRESHOLD
                or rs_percentile < cfg.RS_PCT_THRESHOLD
                or silent_score < cfg.SCORE_WATCH):
            return None

        regime_mult_clamped = max(0.5, min(1.0, regime_mult))
        quality_score = max(0.0, min(100.0, round(float(silent_score) * regime_mult_clamped, 1)))

        stop_distance = atr_val * cfg.ATR_MULTIPLIER
        stop_loss_price = cp - stop_distance
        risk_per_share = cp - stop_loss_price
        target_price = cp + (stop_distance * cfg.RR_RATIO)
        risk_reward = ((target_price - cp) / risk_per_share) if risk_per_share > 0 else 0

        qty_risk = self.max_risk_per_trade / risk_per_share if risk_per_share > 0 else 0
        qty_cap = self.max_capital_per_trade / cp if cp > 0 else 0
        quantity = int(min(qty_risk, qty_cap))
        capital_required = quantity * cp

        pct_from_p50 = ((cp - pivot_50) / pivot_50) * 100 if not pd.isna(pivot_50) and pivot_50 != 0 else 0.0
        pct_from_p250 = ((cp - pivot_250) / pivot_250) * 100 if not pd.isna(pivot_250) and pivot_250 != 0 else np.nan
        ema50_distance = ((cp - ema50) / ema50) * 100 if ema50 else 0.0
        is_breakout = cp > pivot_250 if not pd.isna(pivot_250) else False

        if quality_score >= cfg.SCORE_STRONG:
            signal_strength = "STEALTH LEADER 🕵️"
            recommendation = "BUY TODAY"
        elif quality_score >= cfg.SCORE_QUALIFIED:
            signal_strength = "ACCUMULATING 📈"
            recommendation = "BUY TODAY" if d_metrics.get('Silent_Qualified') else "WATCH"
        else:
            signal_strength = "FORMING 👀"
            recommendation = "WATCH"

        signal = {
            'Symbol': symbol,
            'Sector': self.SECTOR_MAP.get(symbol, 'OTHER'),
            'Horizon': 'SILENT',
            'LTP': round(cp, 2),
            'Score': quality_score,
            'Strength': signal_strength,
            'AI_Summary': d_metrics.get('Silent_Rationale', 'Silent accumulation footprint detected.'),
            'Liquidity': liquidity_label,
            'Trend': "BULL" if cp > ema50 else "BEAR",
            'Distance50': f"{pct_from_p50:.2f}%",
            'Distance250': f"{pct_from_p250:.2f}%" if not pd.isna(pct_from_p250) else "N/A",
            'EMA50_Distance': round(ema50_distance, 2),
            'EMA20_Distance': round(((cp - ema20) / ema20) * 100, 2) if ema20 else 0.0,
            'RS_Pctl': round(rs_percentile, 1),
            'Sector_RS': round(sector_rs, 1),
            'ADX': round(adx_val, 1),
            'RSI': round(rsi_val, 1),
            # Silent setups have no intraday volume burst by construction; the
            # column is kept at 1.0 so downstream risk banding stays comparable.
            'Vol_Ratio': round(float(d_metrics.get('Silent_Volume_Participation', 1.0) or 1.0), 2),
            'Breakout250': "YES" if is_breakout else "NO",
            'Breakout_Quality': 100 if is_breakout else 50,
            'Trigger': round(cp, 2),
            'Stop': round(stop_loss_price, 2),
            'Target': round(target_price, 2),
            'Risk_Reward': round(risk_reward, 2),
            'Qty': quantity,
            'Cap_Req': f"₹{int(capital_required):,}",
            # The daily report groups rows by this field; SILENT has no
            # intraday execution layer, so the grade drives the bucket.
            'Execution_Recommendation': recommendation,
        }
        signal.update({k: v for k, v in d_metrics.items()
                       if k.startswith('Silent_') and k != 'Silent_Components'})
        return signal

    def scan(self, symbol: str, df_daily: pd.DataFrame, df_15min: pd.DataFrame,
             rs_percentile: float = 50.0, sector_rs: float = 50.0, regime_mult: float = 1.0,
             strategy: str = 'SWING', daily_metrics: Optional[Dict[str, Any]] = None,
             df_5min: Optional[pd.DataFrame] = None) -> Optional[Dict[str, Any]]:
        """Runs the multi-factor scoring model and calculates position sizing."""
        d_metrics = daily_metrics if daily_metrics is not None else self.compute_daily_metrics(df_daily)
        if d_metrics is None: return None

        # SILENT is a daily-structure strategy: it deliberately ignores the
        # intraday volume burst the momentum path depends on.
        if strategy.upper() == 'SILENT':
            return self.scan_silent(symbol, df_daily, d_metrics,
                                    rs_percentile=rs_percentile, sector_rs=sector_rs,
                                    regime_mult=regime_mult)

        if len(df_15min) < 3: return None
            
        # Bulletproof .get() methods to prevent KeyErrors
        cp = d_metrics.get('Close', 0)
        ema50 = d_metrics.get('EMA50', cp)
        ema20 = d_metrics.get('EMA20', cp)
        high_price = d_metrics.get('High', cp)
        low_price = d_metrics.get('Low', cp)
        adx_val = d_metrics.get('adx', 0)
        rsi_val = d_metrics.get('RSI', 50)
        atr_val = d_metrics.get('atr', cp * 0.02)
        pivot_50 = d_metrics.get('Pivot_50', cp)
        pivot_250 = d_metrics.get('Pivot_250', cp)
        
        liquidity_val = d_metrics.get('Avg_Traded_Value_20d', 0)
        liquidity_label = "HIGH" if liquidity_val > 100_000_000 else "LOW"
        
        # Hard Filter
        if liquidity_label == "LOW" or cp < ema50 or rs_percentile < config.Discovery.RS_PCT_THRESHOLD or rsi_val < config.Discovery.RSI_THRESHOLD:
            return None

        strategy = strategy.upper()
        is_btst = strategy == 'BTST'
        is_gap = strategy == 'GAP'

        # Volume Mathematics
        recent_vol_avg = df_15min['Volume'].iloc[-3:].mean()
        historical_avg_vol = df_15min['Volume'].iloc[:-3].tail(40).median()
        vol_ratio = (recent_vol_avg / historical_avg_vol) if historical_avg_vol > 0 else 0
        vol_ratio = min(vol_ratio, 6.0)

        close_high_ratio = cp / high_price if high_price > 0 else 0
        close_range_ratio = (cp - low_price) / ((high_price - low_price) + 1e-9)

        # Breakout Checks
        is_breakout = cp > pivot_250 if not pd.isna(pivot_250) else False
        is_near_high = (pivot_250 - cp) / pivot_250 <= 0.03 if not pd.isna(pivot_250) else False
        breakout_bonus = 15 if is_breakout else (8 if is_near_high else 0)
        
        pct_from_p50 = ((cp - pivot_50) / pivot_50) * 100 if not pd.isna(pivot_50) and pivot_50 != 0 else 0.0

        # =========================================
        # PURE MOMENTUM ENGINE (NO PENALTIES)
        # =========================================
        
        # 1. Volume Score (Rewards up to 30 points for high volume)
        volume_score = min(30.0, (vol_ratio / 3.0) * 30.0)

        # 2. ADX Score (Rewards up to 25 points for strong trend velocity)
        adx_score = min(25.0, (adx_val / 40.0) * 25.0)

        # 3. RS Score (Rewards up to 20 points for massive relative strength)
        rs_score = min(20.0, (rs_percentile / 100.0) * 20.0)

        # 4. Sector Score (Rewards up to 10 points for strong sectors)
        sector_score = min(10.0, (sector_rs / 100.0) * 10.0)

        # 5. Breakout Score
        breakout_score = 10 if breakout_bonus > 0 else 0

        # 6. Trend Score
        trend_score = 5 if cp > ema20 else 0

        # Calculate Final Raw Score
        raw_score = volume_score + adx_score + rs_score + sector_score + breakout_score + trend_score
        
        regime_mult_clamped = max(0.5, min(1.0, regime_mult))
        quality_score = raw_score * regime_mult_clamped
        quality_score = max(0, min(100, round(quality_score, 1)))

        beta = self.beta_registry.get(symbol.replace('-EQ', ''), 1.0)
        dynamic_multiplier = max(1.5, self.base_multiplier + (self.alpha * (beta - 1.0)))
        stop_distance = atr_val * (config.Scanner.ATR_MULTIPLIER_BTST if is_btst else dynamic_multiplier)
        stop_loss_price = cp - stop_distance
        
        risk_per_share = cp - stop_loss_price
        qty_risk = self.max_risk_per_trade / risk_per_share if risk_per_share > 0 else 0
        qty_cap = self.max_capital_per_trade / cp if cp > 0 else 0
        target_price = cp + (stop_distance * (config.Scanner.RR_RATIO_BTST if is_btst else config.Scanner.RR_RATIO_SWING))
        risk_reward = ((target_price - cp) / risk_per_share) if risk_per_share > 0 else 0
        ema50_distance = ((cp - ema50) / ema50) * 100 if ema50 else 0.0
        breakout_quality = 100 if is_breakout else (70 if is_near_high else 35)
        
        quantity = int(min(qty_risk, qty_cap))
        capital_required = quantity * cp

        pct_from_p250 = ((cp - pivot_250) / pivot_250) * 100 if not pd.isna(pivot_250) and pivot_250 != 0 else np.nan

        if quality_score >= 80:
            signal_strength = "VERY STRONG 🚀"
        elif quality_score >= 65:
            signal_strength = "STRONG 🔥"
        elif quality_score >= 50:
            signal_strength = "MODERATE ⚡"
        else:
            signal_strength = "WEAK ⚠️"

        reasons = []
        if adx_val > 25: reasons.append(f"robust trend velocity (ADX {adx_val:.1f})")
        if vol_ratio > 2.0: reasons.append(f"institutional volume accumulation ({vol_ratio:.1f}x)")
        if breakout_bonus > 0: reasons.append("a structural 250-day breakout")
        if rs_percentile > 80: reasons.append(f"top-tier relative strength ({rs_percentile:.1f} pctl)")
        if rsi_val > 65: reasons.append(f"bullish momentum expansion (RSI {rsi_val:.1f})")
        
        ai_summary = "Driven by " + ", ".join(reasons) + "." if reasons else "Favorable baseline technicals and solid risk-to-reward ratio."
        execution_metrics = {}
        if is_btst or is_gap:
            execution_metrics = self.compute_intraday_execution_score(df_5min, df_15min)
            ai_summary = self._execution_rationale(ai_summary, execution_metrics, breakout_bonus)

        signal = {
            'Symbol': symbol,
            'Sector': self.SECTOR_MAP.get(symbol, 'OTHER'),
            'Horizon': strategy,
            'LTP': round(cp, 2),
            'Score': quality_score,
            'Strength': signal_strength,
            'AI_Summary': ai_summary,
            'Liquidity': liquidity_label,
            'Trend': "BULL" if cp > ema50 else "BEAR",
            'Close_to_High': round(close_high_ratio, 2),
            'Close_Range': round(close_range_ratio, 2),
            'Distance50': f"{pct_from_p50:.2f}%",
            'Distance250': f"{pct_from_p250:.2f}%" if not pd.isna(pct_from_p250) else "N/A",
            'EMA50_Distance': round(ema50_distance, 2),
            'RS_Pctl': round(rs_percentile, 1),
            'Sector_RS': round(sector_rs, 1),
            'ADX': round(adx_val, 1),
            'RSI': round(rsi_val, 1),
            'Vol_Ratio': round(vol_ratio, 2),
            'Breakout250': "YES" if breakout_bonus > 0 else "NO",
            'Breakout_Quality': breakout_quality,
            'Trigger': round(cp, 2),
            'Stop': round(stop_loss_price, 2),
            'Target': round(target_price, 2),
            'Risk_Reward': round(risk_reward, 2),
            'Qty': quantity,
            'Cap_Req': f"₹{int(capital_required):,}"
        }
        signal.update(execution_metrics)
        return signal

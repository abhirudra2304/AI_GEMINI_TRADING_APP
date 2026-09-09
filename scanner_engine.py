import numpy as np
import pandas as pd
import logging
from datetime import datetime, date
from typing import Optional, Dict, Any
import config
from utils import install_and_import
from mtf_breakout import analyze_mtf_breakout, BREAKOUT_VOLUME_RATIO_MIN
from earnings_manager import EarningsAnalyzer

logger = logging.getLogger(__name__)

# Attempt to install and import pandas-ta. If it fails, ta will be None,
# and functions relying on it will gracefully degrade.
ta = install_and_import('pandas-ta', critical=False)


class BaseScanner:
    """
    A base class for scanners providing common data preparation and metric calculation utilities.
    """
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
        df['Avg_Volume_20d'] = df['Volume'].rolling(20).mean()
        
        # Calculate ATR
        # Use Series.abs() rather than np.abs() so pd.concat() sees Series (not ndarray) operands.
        high_low = df['High'] - df['Low']
        high_close = (df['High'] - df['Close'].shift()).abs()
        low_close = (df['Low'] - df['Close'].shift()).abs()
        df['atr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).ewm(span=14, adjust=False).mean()
        
        # Calculate ADX
        df['up_move'] = df['High'] - df['High'].shift(1)
        df['down_move'] = df['Low'].shift(1) - df['Low']
        # Use pandas .where() to ensure the result is a Series, not a numpy array.
        # This resolves strict-mode type errors with subsequent .ewm() calls.
        df['p_dm'] = df['up_move'].where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), 0)
        df['n_dm'] = df['down_move'].where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), 0)
        p_di = 100 * (df['p_dm'].ewm(alpha=1/14, adjust=False).mean() / df['atr'])
        n_di = 100 * (df['n_dm'].ewm(alpha=1/14, adjust=False).mean() / df['atr'])
        df['adx'] = (100 * (p_di - n_di).abs() / (p_di + n_di + 1e-10)).ewm(alpha=1/14, adjust=False).mean()
        
        # Calculate RSI
        delta = df['Close'].diff()
        # delta.where(delta > 0, 0) maps the leading NaN (from .diff() on row 0) to 0,
        # not NaN - since NaN > 0 is False, the leading row silently becomes a fabricated
        # "zero change" day and skews the Wilder EWM seed. Re-mask it back to NaN so the
        # recursion starts at the first *real* price change, matching pandas-ta's RSI.
        gain = delta.where(delta > 0, 0.0)
        gain.iloc[0] = np.nan
        loss = -delta.where(delta < 0, 0.0)
        loss.iloc[0] = np.nan
        gain = gain.ewm(alpha=1/14, adjust=False).mean()
        loss = loss.ewm(alpha=1/14, adjust=False).mean()
        # Same zero/flat guards as _intraday_rsi below - unguarded gain/loss division
        # only misbehaves on a stretch with zero down-days or zero up-days (rare with
        # real multi-month history, but not impossible), left inconsistent between the
        # two RSI implementations until now.
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        flat = (gain == 0) & (loss == 0)
        rsi = rsi.where(loss != 0, 100)
        rsi = rsi.where(gain != 0, 0)
        rsi = rsi.where(~flat, 50)
        df['RSI'] = rsi
        
        # df.iloc[-1].to_dict() types keys as Hashable; columns are always strings here,
        # so rebuild the dict with explicit str keys to satisfy the Dict[str, Any] contract.
        metrics: Dict[str, Any] = {str(k): v for k, v in df.iloc[-1].to_dict().items()}
        # Previous-bar ADX so downstream scanners can score ADX slope without recomputing.
        metrics['adx_prev'] = float(df['adx'].iloc[-2]) if len(df) >= 2 and pd.notna(df['adx'].iloc[-2]) else np.nan
        return metrics

    def _prepare_intraday_frame(self, df: Optional[pd.DataFrame]) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        intraday = df.copy()
        if 'Timestamp' in intraday.columns:
            intraday['Timestamp'] = pd.to_datetime(intraday['Timestamp'], errors='coerce')
            intraday = intraday.dropna(subset=['Timestamp']).sort_values('Timestamp')
        return intraday.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])

    def _latest_session(self, intraday: pd.DataFrame) -> pd.DataFrame:
        """Returns only the most recent session's candles from a multi-day intraday frame."""
        if intraday.empty or 'Timestamp' not in intraday.columns:
            return intraday
        last_date: date = pd.Timestamp(intraday['Timestamp'].iloc[-1]).date()
        return intraday[pd.to_datetime(intraday['Timestamp']).dt.date == last_date]

class HybridScanner(BaseScanner):
    SECTOR_MAP = config.Universe.SECTOR_MAP

    def __init__(self, base_multiplier: float = 2.0, alpha: float = 0.5, 
                 max_risk_per_trade: float = 5000, max_capital_per_trade: float = 100000,
                 earnings_danger_days: int = 4):
        self.base_multiplier = base_multiplier
        self.alpha = alpha
        self.max_risk_per_trade = max_risk_per_trade 
        self.max_capital_per_trade = max_capital_per_trade
        self.beta_registry = config.Universe.BETA_REGISTRY
        self.earnings_engine = EarningsAnalyzer(danger_zone_days=earnings_danger_days)

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

    def _intraday_rsi(self, close: pd.Series, period: int = 14) -> float:
        if close.empty:
            return 50.0
        delta = close.diff()
        # Same fix as compute_daily_metrics' RSI: re-mask the leading NaN (from .diff() on
        # row 0) back to NaN instead of letting .where(..., 0) turn it into a fabricated
        # "zero change" day, which would skew the Wilder EWM seed.
        gain = delta.where(delta > 0, 0.0)
        gain.iloc[0] = np.nan
        loss = -delta.where(delta < 0, 0.0)
        loss.iloc[0] = np.nan
        gain = gain.ewm(alpha=1/period, adjust=False).mean()
        loss = loss.ewm(alpha=1/period, adjust=False).mean()
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
        if len(df) < period or ta is None:
            if ta is None:
                # The install_and_import function already logs a detailed error on failure.
                # This check ensures we don't proceed if the import failed.
                pass
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

        # Session-relative metrics (VWAP, RSI, volume ratio, closing strength, afternoon
        # momentum) must be measured against *today's* candles only, not the multi-day
        # `primary` frame - otherwise VWAP/RSI/high-of-day blend in prior sessions and the
        # score never reflects today's actual execution quality. EMA/Supertrend intentionally
        # keep the multi-day frame since they need a longer lookback to be meaningful.
        session = self._latest_session(primary)
        if session.empty:
            session = primary

        typical_price = (session['High'] + session['Low'] + session['Close']) / 3
        cumulative_volume = session['Volume'].replace(0, np.nan).cumsum()
        vwap_series = (typical_price * session['Volume']).cumsum() / cumulative_volume
        vwap = float(vwap_series.iloc[-1]) if not pd.isna(vwap_series.iloc[-1]) else price
        above_vwap = price > vwap
        vwap_distance = ((price - vwap) / vwap) * 100 if vwap > 0 else 0.0

        supertrend = self._latest_supertrend(trend_frame)
        above_supertrend = not pd.isna(supertrend) and price > supertrend

        ema5 = primary['Close'].ewm(span=5, adjust=False).mean().iloc[-1]
        ema20 = primary['Close'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = primary['Close'].ewm(span=50, adjust=False).mean().iloc[-1] if len(primary) >= 50 else np.nan
        ema_aligned = not pd.isna(ema50) and ema5 > ema20 and ema20 > ema50

        intraday_rsi = self._intraday_rsi(session['Close'])
        current_volume = float(session['Volume'].iloc[-1])
        avg_volume_20 = np.nan_to_num(session['Volume'].tail(21).iloc[:-1].mean() if len(session) > 20 else session['Volume'].iloc[:-1].mean())
        volume_ratio = current_volume / avg_volume_20 if avg_volume_20 > 0 else 0.0

        intraday_high = float(session['High'].max())
        high_distance = ((intraday_high - price) / intraday_high) * 100 if intraday_high > 0 else 100.0

        afternoon_momentum = False
        if 'Timestamp' in session.columns and not session.empty:
            timestamps = session['Timestamp'].dt.tz_localize(None)
            afternoon = session[timestamps.dt.time >= datetime.strptime("14:30", "%H:%M").time()]
            if len(afternoon) >= 3:
                afternoon_momentum = bool(
                    afternoon['High'].iloc[-1] > afternoon['High'].iloc[0]
                    and afternoon['Low'].iloc[-1] > afternoon['Low'].iloc[0]
                    and afternoon['Close'].iloc[-1] > afternoon['Open'].iloc[-1]
                )

        # Define max points for each component for explainability
        max_points = {
            'vwap': 10, 'supertrend': 10, 'ema_alignment': 10, 'rsi': 10,
            'volume': 10, 'closing_strength': 10, 'vwap_proximity': 5, 'afternoon_momentum': 10
        }

        breakdown = {
            'vwap': max_points['vwap'] if above_vwap else 0,
            'supertrend': max_points['supertrend'] if above_supertrend else 0,
            'ema_alignment': max_points['ema_alignment'] if ema_aligned else 0,
            'rsi': 0,
            'volume': 0,
            'closing_strength': 0,
            'vwap_proximity': 0,
            'afternoon_momentum': max_points['afternoon_momentum'] if afternoon_momentum else 0,
        }

        if 55 <= intraday_rsi <= 75:
            breakdown['rsi'] = max_points['rsi']
        elif 50 <= intraday_rsi < 55:
            breakdown['rsi'] = 5
        elif intraday_rsi > 75:
            breakdown['rsi'] = 3

        if volume_ratio > 1.5:
            breakdown['volume'] = max_points['volume']
        elif volume_ratio > 1.2:
            breakdown['volume'] = 5

        if high_distance < 0.5:
            breakdown['closing_strength'] = max_points['closing_strength']
        elif high_distance <= 1.0:
            breakdown['closing_strength'] = 5

        if 0 <= vwap_distance <= 1:
            breakdown['vwap_proximity'] = max_points['vwap_proximity']

        score = sum(breakdown.values())

        if score >= 65:
            grade = 'A+'
        elif score >= config.ExecutionThresholds.BUY_TODAY_SCORE:
            grade = 'A'
        elif score >= config.ExecutionThresholds.WATCH_SCORE:
            grade = 'B'
        elif score >= 30:
            grade = 'C'
        else:
            grade = 'D'

        momentum = 'Strong' if afternoon_momentum and above_vwap and ema_aligned else ('Constructive' if score >= 42 else 'Weak')
        closing_strength = 'Strong' if high_distance < 0.5 else ('Acceptable' if high_distance <= 1.0 else 'Weak')
        
        # Use thresholds from config for recommendation
        is_buy = score >= config.ExecutionThresholds.BUY_TODAY_SCORE and above_vwap and closing_strength != 'Weak'
        is_watch = score >= config.ExecutionThresholds.WATCH_SCORE
        recommendation = 'BUY TODAY' if is_buy else ('WATCH' if is_watch else 'WAIT')

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
            '_Execution_Score_Breakdown': breakdown,
            '_Execution_Max_Points': max_points,
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
        return "Strong daily trend but weak intraday execution. Wait for better entry."

    @staticmethod
    def _same_bucket_history(df_15min: pd.DataFrame) -> tuple[Optional[pd.Series], Optional[pd.Series], Optional[Any]]:
        """Shared setup for both bucketed-baseline methods below: naive
        (tz-stripped) date/time Series aligned to df_15min, plus today's date.

        Returns (dates, times, today) - each None if df_15min lacks a usable
        Timestamp column, so callers can fall back without special-casing tz.
        """
        if df_15min is None or df_15min.empty or 'Timestamp' not in df_15min.columns:
            return None, None, None
        ts = df_15min['Timestamp']
        ts_naive = ts.dt.tz_localize(None) if ts.dt.tz is not None else ts
        dates = ts_naive.dt.date
        times = ts_naive.dt.time
        if dates.empty:
            return None, None, None
        return dates, times, dates.iloc[-1]

    def _bucketed_volume_baseline(self, df_15min: pd.DataFrame, recent_slice: pd.DataFrame, lookback_days: int = 5) -> float:
        """Same-time-of-day baseline for `recent_slice`'s candles: for each
        candle's 15-min bucket (e.g. 12:45-13:00), the median volume other
        prior trading sessions (never today) had in that same bucket over
        the last `lookback_days` sessions, averaged across recent_slice's
        buckets to stay comparable in scale to a mean-of-3-candles reading.

        Replaces the old `iloc[:-3].tail(40).median()` baseline, which
        blended candles from every time of day across multiple days with no
        time-of-day alignment - since NSE intraday volume follows a U-shape
        (heavy at open/close, quiet at midday), that baseline made Vol_Ratio
        mechanically rise through the session for every stock regardless of
        genuine relative volume (see 2026-07-30 investigation: every ticker's
        Vol_Ratio roughly tripled to sextupled between a 12:57 and a 15:27
        scan on the same day).

        Args:
            df_15min: Multi-day 15-min OHLCV, DatetimeIndex-free (Timestamp
                column), sorted ascending.
            recent_slice: The candles being evaluated (e.g. the last 3),
                a sub-frame of df_15min.
            lookback_days: Prior trading sessions to draw the baseline from.
                Needs only 3-5 sessions of history, not a full historical
                curve.

        Returns:
            0.0 if there's no usable Timestamp data or no prior-day
            candles in any of recent_slice's buckets (e.g. a newly-listed
            stock) - caller's existing expected-volume floor covers that.
        """
        dates, times, today = self._same_bucket_history(df_15min)
        if dates is None or recent_slice.empty:
            return 0.0

        recent_ts = recent_slice['Timestamp']
        recent_ts_naive = recent_ts.dt.tz_localize(None) if recent_ts.dt.tz is not None else recent_ts

        bucket_medians = []
        for bucket_time in recent_ts_naive.dt.time:
            same_bucket = df_15min.loc[(times == bucket_time) & (dates != today), 'Volume']
            if same_bucket.empty:
                continue
            bucket_medians.append(same_bucket.tail(lookback_days).median())

        return float(pd.Series(bucket_medians).mean()) if bucket_medians else 0.0

    def _bucketed_daily_volume_ratio(
        self, df_15min: pd.DataFrame, today_volume_so_far: float, fallback_avg_volume_20d: float, lookback_days: int = 5
    ) -> float:
        """BTST's cumulative-volume-so-far ratio, normalized against the
        median *cumulative* volume other recent sessions had reached by
        this same time of day - not a full-day average, which mechanically
        rises all session long since today_volume_so_far only grows while
        Avg_Volume_20d (a full-day figure) stays fixed. Same root cause and
        fix philosophy as `_bucketed_volume_baseline` above, applied to a
        cumulative total instead of a windowed snapshot.

        Falls back to `today_volume_so_far / fallback_avg_volume_20d` (the
        original formula) when there's no usable intraday history to build
        a same-time-of-day baseline from - e.g. a newly-listed stock.

        Args:
            df_15min: Multi-day 15-min OHLCV (same frame `scan()` already
                has), used only to reconstruct prior sessions' cumulative
                volume-by-this-time - never today's own candles.
            today_volume_so_far: Today's cumulative volume
                (daily_metrics['Volume'], still-forming today's bar).
            fallback_avg_volume_20d: Original full-day-average baseline,
                used verbatim when the bucketed baseline can't be built.
            lookback_days: Prior trading sessions to draw the baseline from.

        Returns:
            1.0 (neutral) if fallback_avg_volume_20d is also unavailable.
        """
        fallback = (today_volume_so_far / fallback_avg_volume_20d) if fallback_avg_volume_20d else 1.0

        dates, times, today = self._same_bucket_history(df_15min)
        if dates is None:
            return fallback

        current_time = times.iloc[-1]
        prior_dates = sorted(d for d in set(dates) if d != today)[-lookback_days:]
        if not prior_dates:
            return fallback

        cumulative_by_day = [
            df_15min.loc[(dates == d) & (times <= current_time), 'Volume'].sum()
            for d in prior_dates
        ]
        baseline = float(pd.Series(cumulative_by_day).median())
        return (today_volume_so_far / baseline) if baseline > 0 else fallback

    def _compute_btst_score(self, daily_metrics: Dict[str, Any], rs_percentile: float, sector_rs: float, vol_ratio: float) -> tuple[float, str]:
        """
        Computes a dedicated score for BTST that is less sensitive to intraday execution noise.
        This score prioritizes daily chart strength, breakout quality, and closing power.
        """
        if not daily_metrics:
            return 0.0, "Missing daily metrics"

        # Weights: Daily Breakout (25), RS/Sector (25), Daily Momentum (25), Closing Strength (25)
        
        # 1. Breakout & Trend Quality (25 points)
        cp = float(daily_metrics.get('Close', 0))
        pivot_250_raw = daily_metrics.get('Pivot_250', cp)
        pivot_250: float = float(pivot_250_raw) if pivot_250_raw is not None else float('nan')
        is_breakout = pd.notna(pivot_250) and cp > pivot_250
        breakout_score = 25 if is_breakout else 0

        # 2. Relative Strength (25 points)
        rs_score = (rs_percentile / 100.0) * 15  # 15 points for stock RS
        sector_score = (sector_rs / 100.0) * 10 # 10 points for sector RS

        # 3. Daily Momentum (25 points)
        adx_val = float(daily_metrics.get('adx', 0))
        adx_score = min(10.0, ((max(0, adx_val - 20)) / 20.0) * 10.0) # 10 points for ADX > 20
        volume_score = min(15.0, (vol_ratio / 3.0) * 15.0) # 15 points for volume

        # 4. Closing Strength (25 points)
        high = float(daily_metrics.get('High', cp))
        low = float(daily_metrics.get('Low', cp))
        close_range_ratio = (cp - low) / ((high - low) + 1e-9)
        closing_score = close_range_ratio * 25

        final_score = breakout_score + rs_score + sector_score + adx_score + volume_score + closing_score
        return min(100.0, final_score), "BTST Score based on daily structure"

    def _apply_mtf_override(self, signal: Dict[str, Any], df_daily: pd.DataFrame,
                             df_15min: pd.DataFrame, rr_ratio: float,
                             now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Real-time MTF override: the EOD engine above sets the base Strength/Stop/Target
        from daily closes alone, so it can label a stock "Watch only" the same morning
        it's breaking out live. If the daily engine already flagged a structural
        breakout AND the 15m tape confirms it on volume with a valid tighter stop,
        force-promote the signal and swap the stop to the 15m SuperTrend. The macro
        scoring above is left completely untouched either way - this only ever
        overrides the fields below, and only when every gate condition holds.

        Field-name note: this reads/writes the scanner's actual signal keys
        (`Breakout250`, `Stop`/`stop`) rather than `Breakout_Status`/`Stop_Loss` -
        those keys don't exist anywhere in the signal dict `scan()` builds, so
        gating on them would silently never fire.

        `now` is exposed purely so the Fractional Candle Guard inside
        analyze_mtf_breakout() is deterministically testable without patching
        the system clock; production callers can leave it as None.
        """
        mtf = analyze_mtf_breakout(df_daily, df_15min, now=now)

        # Shadow stamping — always recorded for audit transparency, independent
        # of whether the override gate below actually fires.
        signal['MTF_Breakout_Status'] = mtf.get('Breakout_Status', 'NO')
        signal['MTF_Vol_Ratio_15m'] = mtf.get('Vol_Ratio_15m', 0.0)
        signal['MTF_Macro_Resistance'] = mtf.get('Macro_Resistance', np.nan)
        signal['MTF_Triggered'] = False

        live_price = mtf.get('LTP')
        st_15m_val = mtf.get('Intraday_SuperTrend_Stop')
        vol_ratio_15m = mtf.get('Vol_Ratio_15m', 0.0)

        # --- The Override Gateway (all three must hold) ---
        daily_breakout_confirmed = signal.get('_True_Breakout250', False)
        if not daily_breakout_confirmed:
            return signal
        if live_price is None or pd.isna(live_price) or st_15m_val is None or pd.isna(st_15m_val):
            return signal
        cp = float(live_price)
        new_stop = float(st_15m_val)
        if new_stop >= cp:  # invalid/untightened 15m stop
            return signal
        if vol_ratio_15m < BREAKOUT_VOLUME_RATIO_MIN:
            return signal
        stop_distance = cp - new_stop
        target_price = cp + (stop_distance * rr_ratio)
        risk_reward = (target_price - cp) / stop_distance
        qty_risk = self.max_risk_per_trade / stop_distance
        qty_cap = self.max_capital_per_trade / cp if cp > 0 else 0
        quantity = int(min(qty_risk, qty_cap))
        capital_required = quantity * cp

        signal['MTF_Triggered'] = True
        signal['LTP'] = round(cp, 2)
        signal['Trigger'] = round(cp, 2)
        signal['entry'] = round(cp, 2)
        signal['Stop'] = round(new_stop, 2)
        signal['stop'] = round(new_stop, 2)
        signal['Target'] = round(target_price, 2)
        signal['target'] = round(target_price, 2)
        signal['Risk_Reward'] = round(risk_reward, 2)
        signal['Qty'] = quantity
        signal['Cap_Req'] = f"₹{int(capital_required):,}"
        # Re-use the existing "VERY STRONG" enum value (rather than inventing a new one)
        # so every downstream consumer that pattern-matches Strength keeps working
        # unmodified; the AI_Summary note is what actually explains the promotion.
        signal['Strength'] = "VERY STRONG 🚀"
        signal['Execution_Recommendation'] = 'BUY TODAY'
        signal['AI_Summary'] = (
            f"🚨 MTF INTRADAY TRIGGER: Daily breakout confirmed by 15m tape — "
            f"price cleared macro resistance ({mtf.get('Macro_Resistance')}) on "
            f"{vol_ratio_15m}x 15m volume. Stop tightened to the 15m SuperTrend. "
        ) + signal.get('AI_Summary', '')
        return signal

    def scan(self, symbol: str, df_daily: pd.DataFrame, df_15min: pd.DataFrame,
             rs_percentile: float = 50.0, sector_rs: float = 50.0, regime_mult: float = 1.0,
             strategy: str = 'SWING', daily_metrics: Optional[Dict[str, Any]] = None,
             df_5min: Optional[pd.DataFrame] = None, live_close: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Runs the multi-factor scoring model and calculates position sizing."""
        d_metrics = daily_metrics if daily_metrics is not None else self.compute_daily_metrics(df_daily)
        if d_metrics is None or len(df_15min) < 3:
            logger.debug(
                "scan: %s hard-filtered - insufficient data (daily_metrics=%s, len(df_15min)=%d)",
                symbol, "missing" if d_metrics is None else "ok", len(df_15min),
            )
            return None

        # Bulletproof .get() methods to prevent KeyErrors
        # `live_close` (the last 15m close, fetched fresh every scan cycle) takes
        # priority over d_metrics['Close'], which is whatever Discovery captured on
        # its own cache cadence (up to CacheConfig.CACHE_REFRESH_MINUTES old) and
        # never refreshed by the fast execution scan path - without this, LTP/
        # Trigger/Stop/Target stay frozen at the Discovery-time price across every
        # fast-scan cycle even though Vol_Ratio (sourced from this same df_15min)
        # updates live, producing a stale-price/live-volume mismatch in the score.
        # Callers only pass live_close when df_15min is genuinely live (see
        # orchestrator.py's scan_stock): backtests deliberately leave it None so
        # point-in-time price stays sourced from Discovery's point_in_time-bounded
        # fetch, not from get_live_candles() (which has no point_in_time concept).
        if live_close is not None and not pd.isna(live_close) and live_close > 0:
            cp = float(live_close)
        else:
            cp = float(d_metrics.get('Close', 0))
        ema50 = float(d_metrics.get('EMA50', cp))
        ema20 = float(d_metrics.get('EMA20', cp))
        high_price = float(d_metrics.get('High', cp))
        low_price = float(d_metrics.get('Low', cp))
        adx_val = float(d_metrics.get('adx', 0))
        rsi_val = d_metrics.get('RSI', 50)
        atr_val = d_metrics.get('atr', cp * 0.02)
        pivot_50 = d_metrics.get('Pivot_50', cp)
        pivot_250 = d_metrics.get('Pivot_250', cp)
        
        liquidity_val = d_metrics.get('Avg_Traded_Value_20d', 0)
        liquidity_label = "HIGH" if liquidity_val > 100_000_000 else "LOW"
        
        # Hard Filter - each condition logged individually (debug) so a dropped
        # ticker's exact rejection reason is traceable instead of a bare None
        # (see orchestrator.py's top-N drop-reason diffing, 2026-07-30).
        failed_filters = []
        if liquidity_label == "LOW":
            failed_filters.append("liquidity")
        if cp < ema50:
            failed_filters.append("trend (cp < ema50)")
        if rs_percentile < config.Discovery.RS_PCT_THRESHOLD:
            failed_filters.append(f"RS percentile ({rs_percentile:.1f} < {config.Discovery.RS_PCT_THRESHOLD})")
        if rsi_val < config.Discovery.RSI_THRESHOLD:
            failed_filters.append(f"RSI ({rsi_val:.1f} < {config.Discovery.RSI_THRESHOLD})")
        if failed_filters:
            logger.debug("scan: %s hard-filtered - failed: %s", symbol, ", ".join(failed_filters))
            return None

        strategy = strategy.upper()
        is_btst = strategy == 'BTST'
        is_gap = strategy == 'GAP'
        is_intraday = strategy == 'INTRADAY'

        # Volume Mathematics
        # Calculate a dynamic baseline volume floor to avoid skewed ratios in illiquid stocks
        expected_daily_shares = liquidity_val / cp if cp > 0 else 0
        expected_15m_vol = expected_daily_shares / 25 # Assuming ~25 15-min candles per day
        
        recent_slice = df_15min.iloc[-3:]
        recent_vol_avg = recent_slice['Volume'].mean()
        historical_median_vol = np.nan_to_num(self._bucketed_volume_baseline(df_15min, recent_slice))
        
        # The baseline is the greater of the actual historical median or 20% of the expected volume
        baseline_vol = max(float(historical_median_vol), expected_15m_vol * 0.20)
        vol_ratio = (recent_vol_avg / baseline_vol) if baseline_vol > 0 else 0.0
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

        # 2. ADX Score (Rewards up to 25 points for strong trend velocity, with a floor at 20)
        if adx_val < 20:
            adx_score = 0.0
        else:
            # Scale the range from 20-40 into the 0-25 point score
            adx_score = min(25.0, ((adx_val - 20) / 20.0) * 25.0)

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
        
        # regime_mult_clamped is applied to position sizing only (see `quantity`
        # below), not to quality_score/tiering - redesigned 2026-07-30: multiplying
        # a fixed regime dampening into the score before tiering made MODERATE+
        # mathematically unreachable in weaker regimes (quality_score capped at
        # raw_score_max * mult, e.g. exactly 50 in EXTREME_BEAR's 0.5x - precisely
        # the old WEAK/MODERATE boundary), hiding genuinely strong setups instead
        # of correctly grading them lower. Regime now affects how much to risk on
        # a signal, not whether it's recognized as a good signal at all.
        regime_mult_clamped = max(0.5, min(1.0, regime_mult))
        quality_score = max(0, min(100, round(raw_score, 1)))

        # Full per-component breakdown for every scored ticker - makes "why did X
        # outscore Y despite worse RS/RSI/Vol_Ratio" answerable from logs alone,
        # without re-deriving the formula by hand each time (see 2026-07-30
        # COFORGE-vs-ALKEM investigation). Debug-level: not meant for normal
        # terminal output, only pulled up when actually needed.
        logger.debug(
            "scan: %s score=%.1f (volume=%.1f, adx=%.1f, rs=%.1f, sector=%.1f, "
            "breakout=%.1f, trend=%.1f, regime_mult=%.2f, raw=%.1f)",
            symbol, quality_score, volume_score, adx_score, rs_score, sector_score,
            breakout_score, trend_score, regime_mult_clamped, raw_score,
        )

        beta = self.beta_registry.get(symbol.replace('-EQ', ''), 1.0)
        dynamic_multiplier = max(1.5, self.base_multiplier + (self.alpha * (beta - 1.0)))

        # Simplified logic for multipliers and ratios
        if is_btst:
            atr_multiplier = config.Scanner.ATR_MULTIPLIER_BTST
            rr_ratio = config.Scanner.RR_RATIO_BTST
        elif is_intraday:
            atr_multiplier = config.Scanner.ATR_MULTIPLIER_INTRADAY
            rr_ratio = config.Scanner.RR_RATIO_INTRADAY
        else: # SWING, GAP
            atr_multiplier = dynamic_multiplier
            rr_ratio = config.Scanner.RR_RATIO_SWING

        stop_distance = atr_val * atr_multiplier
        stop_loss_price = cp - stop_distance
        
        risk_per_share = cp - stop_loss_price
        qty_risk = self.max_risk_per_trade / risk_per_share if risk_per_share > 0 else 0
        qty_cap = self.max_capital_per_trade / cp if cp > 0 else 0
        target_price = cp + (stop_distance * rr_ratio)
        risk_reward = ((target_price - cp) / risk_per_share) if risk_per_share > 0 else 0
        ema50_distance = ((cp - ema50) / ema50) * 100 if ema50 else 0.0
        breakout_quality = 100 if is_breakout else (70 if is_near_high else 35)

        # regime_mult_clamped now lands here instead of quality_score (see note
        # above) - dampens position size in weaker regimes without hiding the
        # underlying setup quality from tiering.
        quantity = int(min(qty_risk, qty_cap) * regime_mult_clamped)
        capital_required = quantity * cp

        pct_from_p250 = ((cp - pivot_250) / pivot_250) * 100 if not pd.isna(pivot_250) and pivot_250 != 0 else np.nan

        # VALIDATED_BEAR_REGIMES_ONLY (2026-07-30): thresholds re-derived against
        # raw_score (pre-regime-multiply, see the redesign note above `quality_score`
        # near `regime_mult_clamped`'s first use). Anchored on BEARISH_RECOVERY's own
        # old-design tier proportions - the one regime where the old post-multiply
        # design could reach every tier at all - via a 6-date/1154-point paired
        # old/new-Vol_Ratio backtest, then validated to reproduce a similar (not
        # identical) shape under EXTREME_BEAR: no tier is structurally unreachable
        # anymore. Every date in that dataset was EXTREME_BEAR or BEARISH_RECOVERY -
        # no BULLISH/NEUTRAL-regime data exists in the current cache to validate
        # against; re-check both cutoffs the first time such data is available.
        #
        # STRONG+ is intentionally not split into STRONG/VERY STRONG - insufficient
        # anchor data across available regimes (zero real examples in either regime
        # to derive a split from). Split when BULLISH/NEUTRAL regime data becomes
        # available in cache. VERY STRONG labels in reports come exclusively from
        # the MTF intraday override (_apply_mtf_override), not from this threshold.
        if quality_score >= 89.1:
            signal_strength = "STRONG+ 🔥"
            logger.debug(
                "scan: %s tiered STRONG+ at quality_score=%.1f via the "
                "VALIDATED_BEAR_REGIMES_ONLY 89.1 threshold.", symbol, quality_score,
            )
        elif quality_score >= 74.9:
            signal_strength = "MODERATE ⚡"
            logger.debug(
                "scan: %s tiered MODERATE at quality_score=%.1f via the "
                "VALIDATED_BEAR_REGIMES_ONLY 74.9 threshold.", symbol, quality_score,
            )
        else:
            signal_strength = "WEAK ⚠️"

        reasons: list[str] = []
        if adx_val > 25: reasons.append(f"robust trend velocity (ADX {adx_val:.1f})")
        if vol_ratio > 2.0: reasons.append(f"institutional volume accumulation ({vol_ratio:.1f}x)")
        if breakout_bonus > 0: reasons.append("a structural 250-day breakout")
        if rs_percentile > 80: reasons.append(f"top-tier relative strength ({rs_percentile:.1f} pctl)")
        if rsi_val > 65: reasons.append(f"bullish momentum expansion (RSI {rsi_val:.1f})")
        
        ai_summary = "Driven by " + ", ".join(reasons) + "." if reasons else "Favorable baseline technicals and solid risk-to-reward ratio."
        execution_metrics = {}
        if is_gap or is_intraday: 
            # Ensure df_5min is a DataFrame, not None, to satisfy the callee's signature.
            # This narrows the type from Optional[pd.DataFrame] to pd.DataFrame.
            df_5min_safe = df_5min if df_5min is not None else pd.DataFrame()
            execution_metrics = self.compute_intraday_execution_score(df_5min_safe, df_15min)
            ai_summary = self._execution_rationale(ai_summary, execution_metrics, breakout_bonus)
        elif is_btst:
            # BTST uses a different, less intraday-sensitive scoring model.
            avg_volume_20d = d_metrics.get('Avg_Volume_20d', 0)
            daily_vol_ratio = self._bucketed_daily_volume_ratio(df_15min, d_metrics.get('Volume', 0), avg_volume_20d)
            btst_score, btst_reason = self._compute_btst_score(d_metrics, rs_percentile, sector_rs, daily_vol_ratio)
            # BTST has no intraday BUY TODAY/WATCH/WAIT verdict (it ranks by BTST_Score /
            # BTST_Final_Score instead), but every signal must carry Execution_Recommendation
            # so downstream reporting can filter df_signals uniformly across strategies.
            execution_metrics = {'BTST_Score': btst_score, 'Execution_Recommendation': 'N/A'}
            if btst_score > 60:
                ai_summary = f"Strong EOD structure for potential overnight gap. {btst_reason}."
            else:
                ai_summary = f"Moderate EOD structure. {btst_reason}."
        else:
            execution_metrics = {
                'Execution_Score': 0,
                'Execution_Grade': 'N/A',
                'VWAP': 'N/A',
                'Above_VWAP': 'N/A',
                'Intraday_Trend': 'N/A',
                'Closing_Strength': 'N/A',
                'Execution_Recommendation': 'N/A', # This is the key fix
            }

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
            '_True_Breakout250': is_breakout,
            'Breakout_Quality': breakout_quality,
            'Trigger': round(cp, 2),
            'Stop': round(stop_loss_price, 2),
            'Target': round(target_price, 2),
            'Risk_Reward': round(risk_reward, 2),
            'Qty': quantity,
            'Cap_Req': f"₹{int(capital_required):,}",
            # Add lowercase aliases for backtester compatibility (matches EMFBScanner)
            'entry': round(cp, 2),
            'stop': round(stop_loss_price, 2),
            'target': round(target_price, 2),
        }
        signal.update(execution_metrics)
        signal = self._apply_mtf_override(signal, df_daily, df_15min, rr_ratio)

        # --- EXECUTIVE VETO: EARNINGS RISK ANALYZER ---
        # Fetch the risk profile using the cached manager. history_days lets
        # evaluate_risk distinguish a genuinely new/recently-listed ticker (no
        # track record to fall back on) from an established one with merely
        # thin analyst coverage (the CYIENTDLM-class gap) - None (df_daily
        # missing/empty) is treated as "too little history" too, not skipped.
        history_days = len(df_daily) if df_daily is not None else None
        earnings_data = self.earnings_engine.evaluate_risk(symbol, history_days=history_days)

        # Stamp the transparency fields so they show up in reports
        signal['Earnings_Date'] = earnings_data['Earnings_Date']
        signal['Days_To_Earnings'] = earnings_data['Days_To_Earnings']
        signal['Earnings_Risk'] = earnings_data['Earnings_Risk']

        # Apply the Veto: block the signal if earnings are imminent, regardless of
        # strategy. BTST/SWING never carry 'BUY TODAY' (only GAP/INTRADAY do via
        # compute_intraday_execution_score), so gating on that value alone let
        # earnings-risk BTST/SWING picks through unflagged. Fire on HIGH RISK for
        # every strategy instead.
        if "HIGH RISK" in earnings_data['Earnings_Risk']:
            signal['Execution_Recommendation'] = 'AVOID (EARNINGS)'
            signal['Strength'] = 'EVENT RISK 🛑'
            if "New/Recent Listing" in earnings_data['Earnings_Risk']:
                warning_msg = "[🛑 VETO: New/recent listing with no earnings history - cannot verify earnings safety.] "
            else:
                warning_msg = f"[🛑 VETO: Setup invalidated. Earnings in {earnings_data['Days_To_Earnings']} days. Avoid binary risk!] "
            signal['AI_Summary'] = warning_msg + signal.get('AI_Summary', '')
        elif "UNKNOWN" in earnings_data['Earnings_Risk']:
            # Not a veto - the data source (Yahoo Finance) has no earnings date for
            # this symbol, which is NOT the same as confirming no event is imminent
            # (see earnings_manager.py). Surface it so it doesn't silently read the
            # same as a genuinely cleared/SAFE result.
            note_msg = "[❓ Earnings date unknown - verify manually before entry] "
            signal['AI_Summary'] = note_msg + signal.get('AI_Summary', '')

        return signal


class EMFBScanner(BaseScanner):
    """
    An institutional-grade scanner for the Emerging Momentum / Fresh Breakout (EMFB) strategy.
    It uses a two-stage, universe-aware ranking architecture.

    - Stage 1 (`compute_emfb_metrics`): Calculates a rich set of raw, un-scored
      metrics for a single stock.
    - Stage 2 (`rank_and_score_emfb`): Takes the full universe's metrics, converts
      them to percentile ranks, and computes a final weighted score.
    """
    LAST_HOUR_START = datetime.strptime("14:30", "%H:%M").time()

    def __init__(self):
        super().__init__()
        # EMFBScanner is a direct BaseScanner subclass (a sibling of HybridScanner,
        # not a subclass of it), so it doesn't inherit HybridScanner's earnings_engine -
        # needed here for the same evaluate_risk() call compute_emfb_metrics makes.
        self.earnings_engine = EarningsAnalyzer()

    def _compute_indicators(self, df: pd.DataFrame, prefix: str) -> pd.DataFrame:
        """Computes a standard set of indicators for a given timeframe."""
        if df.empty: return df
        df = df.copy()
        df[f'{prefix}_ema20'] = df['Close'].ewm(span=20, adjust=False).mean()
        if len(df) > 50:
            df[f'{prefix}_ema50'] = df['Close'].ewm(span=50, adjust=False).mean()
        if ta:
            bbands = ta.bbands(df['Close'], length=20)
            if bbands is not None and not bbands.empty:
                bbu_col = next(c for c in bbands.columns if c.startswith('BBU_'))
                bbl_col = next(c for c in bbands.columns if c.startswith('BBL_'))
                bbm_col = next(c for c in bbands.columns if c.startswith('BBM_'))
                df[f'{prefix}_bband_width'] = (bbands[bbu_col] - bbands[bbl_col]) / bbands[bbm_col]
        return df

    def compute_emfb_metrics(self, symbol: str, data: Dict[str, Any], nifty_df: pd.DataFrame,
                             sector_df: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        [STAGE 1] Computes all raw metrics for a single stock.
        This function is designed to be fast and run in parallel. It does no scoring.
        """
        # --- Data Validation and Preparation ---
        # 15min is the intended intraday source, but its fetch has historically
        # been the single point of failure that silently dropped an otherwise-
        # healthy symbol from the whole report (2026-09-01: no incremental
        # cache fallback on that timeframe, so one rate-limited API call meant
        # `return None` here with no trace at the report level). data_broker.py
        # now gives 15min its own incremental fallback, but 5min covers the
        # same session at finer granularity and every metric below is computed
        # from session-level aggregates (_prepare_intraday_frame/_latest_session
        # are bar-width-agnostic), so it's a safe substitute if 15min is still
        # empty. Only drop the symbol if BOTH intraday sources failed.
        if data.get('daily') is None or data['daily'].empty:
            return None
        intraday_tf = '15min' if data.get('15min') is not None and not data['15min'].empty else '5min'
        if data.get(intraday_tf) is None or data[intraday_tf].empty:
            return None

        df_daily = self._compute_indicators(data['daily'], 'd')
        df_15min_full = self._prepare_intraday_frame(data[intraday_tf])
        session_15min = self._latest_session(df_15min_full)

        if len(df_daily) < 50 or session_15min.empty:
            return None

        daily_metrics = self.compute_daily_metrics(df_daily)
        if not daily_metrics: return None

        # --- Earnings Risk (see BaseScanner.scan's identical use of evaluate_risk) ---
        # df_daily is already fetched above for indicators/RS, so history_days costs
        # no extra API call. Stashed here (Stage 1) so rank_and_score_emfb (Stage 2)
        # can annotate the Reason field without needing to re-derive it.
        earnings_data = self.earnings_engine.evaluate_risk(symbol, history_days=len(df_daily))

        # --- Metric Calculation ---
        metrics: Dict[str, Any] = {
            'Symbol': symbol,
            'Sector': data.get('sector', 'OTHER'),
            'Earnings_Date': earnings_data['Earnings_Date'],
            'Days_To_Earnings': earnings_data['Days_To_Earnings'],
            'Earnings_Risk': earnings_data['Earnings_Risk'],
        }

        # Daily Metrics
        metrics.update({
            'adx': float(daily_metrics.get('adx', 0)),
            'rsi': float(daily_metrics.get('RSI', 50)),
            'atr': float(daily_metrics.get('atr', 0)),
            'close': float(daily_metrics.get('Close', 0)),
        })

        # --- Intraday Session Metrics (for stop loss calculation later) ---
        # Relative Strength (vs Nifty & Sector)
        for days in [1, 5, 10]:
            if len(df_daily) > days and len(nifty_df) > days:
                stock_ret = (df_daily['Close'].iloc[-1] / df_daily['Close'].iloc[-days - 1]) - 1
                nifty_ret = (nifty_df['Close'].iloc[-1] / nifty_df['Close'].iloc[-days - 1]) - 1
                metrics[f'rs_nifty_{days}d'] = stock_ret - nifty_ret

        if sector_df is not None and not sector_df.empty and len(sector_df) > 1:
            stock_ret_1d = (df_daily['Close'].iloc[-1] / df_daily['Close'].iloc[-2]) - 1
            sector_ret_1d = (sector_df['Close'].iloc[-1] / sector_df['Close'].iloc[-2]) - 1
            metrics['rs_sector_1d'] = stock_ret_1d - sector_ret_1d

        # Intraday Session Metrics
        session_close = session_15min['Close'].iloc[-1]
        session_low = session_15min['Low'].min()
        session_high = session_15min['High'].max()
        session_open = session_15min['Open'].iloc[0]
        metrics['session_low'] = session_low

        # VWAP
        typical_price = (session_15min['High'] + session_15min['Low'] + session_15min['Close']) / 3
        cumulative_volume = session_15min['Volume'].replace(0, np.nan).cumsum()
        vwap_series = (typical_price * session_15min['Volume']).cumsum() / cumulative_volume
        vwap = vwap_series.iloc[-1] if not vwap_series.empty and pd.notna(vwap_series.iloc[-1]) else session_close
        metrics['vwap_distance_pct'] = ((session_close - vwap) / vwap) * 100 if vwap > 0 else 0

        # Recovery & Closing Strength
        drawdown = session_open - session_low
        metrics['recovery_pct'] = ((session_close - session_low) / drawdown) * 100 if drawdown > 0 else (100 if session_close >= session_open else 0)
        session_range = session_high - session_low
        metrics['closing_strength_pct'] = ((session_close - session_low) / session_range) * 100 if session_range > 0 else 100

        # Last Hour Volume & Momentum
        today_lh = session_15min[session_15min['Timestamp'].dt.time >= self.LAST_HOUR_START]
        lh_volume = today_lh['Volume'].sum()
        today_date: date = pd.Timestamp(session_15min['Timestamp'].iloc[0]).date()
        prior_lh_vol = df_15min_full[
            (df_15min_full['Timestamp'].dt.time >= self.LAST_HOUR_START)
            & (df_15min_full['Timestamp'].dt.date < today_date)
        ]
        prior_lh_dates = prior_lh_vol['Timestamp'].dt.date
        avg_prior_lh_vol = prior_lh_vol.groupby(prior_lh_dates)['Volume'].sum().mean()
        metrics['last_hour_vol_ratio'] = lh_volume / avg_prior_lh_vol if avg_prior_lh_vol > 0 else 0
        if len(today_lh) > 1:
            metrics['last_hour_return'] = (today_lh['Close'].iloc[-1] / today_lh['Open'].iloc[0]) - 1 if today_lh['Open'].iloc[0] > 0 else 0

        # Market Survivor Index (Composite)
        green_on_red = 1 if (metrics['close'] > df_daily['Open'].iloc[-1] and nifty_df['Close'].pct_change().iloc[-1] < 0) else 0
        metrics['market_survivor_raw'] = (
            (metrics.get('rs_nifty_1d', 0) * 100) +
            (metrics.get('recovery_pct', 0) / 10) +
            (metrics.get('closing_strength_pct', 0) / 10) +
            (green_on_red * 20)
        )

        # Breakout Quality
        daily_range = df_daily['High'] - df_daily['Low']
        is_nr7 = daily_range.iloc[-1] <= daily_range.iloc[-7:-1].min()
        bband_width = df_daily['d_bband_width'].iloc[-1] if 'd_bband_width' in df_daily.columns else np.nan
        is_squeeze = not pd.isna(bband_width) and bband_width <= df_daily['d_bband_width'].rolling(120).quantile(0.1).iloc[-1]
        breakout_score = 0
        if is_squeeze: breakout_score += 40
        if is_nr7: breakout_score += 30
        if metrics['close'] > daily_metrics.get('Pivot_50', metrics['close']): breakout_score += 30
        metrics['breakout_quality'] = breakout_score

        return metrics

    def rank_and_score_emfb(self, metrics_df: pd.DataFrame, market_regime: str) -> pd.DataFrame:
        """
        [STAGE 2] Ranks, scores, and filters the entire universe of stocks.
        """
        if metrics_df.empty:
            return pd.DataFrame()

        # --- Percentile Ranking ---
        # Higher is better for these metrics
        for col in ['rs_nifty_1d', 'rs_nifty_5d', 'rs_nifty_10d', 'rs_sector_1d',
                    'recovery_pct', 'closing_strength_pct', 'vwap_distance_pct',
                    'last_hour_vol_ratio', 'last_hour_return', 'market_survivor_raw',
                    'breakout_quality', 'rsi', 'adx']:
            if col in metrics_df.columns:
                metrics_df[f'{col}_rank'] = metrics_df[col].rank(pct=True) * 100

        # --- Dynamic Weighting ---
        weights = config.EMFB.WEIGHT_PROFILES.get(market_regime, config.EMFB.WEIGHT_PROFILES['DEFAULT'])
        
        # Rename 'vwap_distance_pct_rank' to 'vwap_rank' to match weights config
        if 'vwap_distance_pct_rank' in metrics_df.columns:
            metrics_df.rename(columns={'vwap_distance_pct_rank': 'vwap_rank'}, inplace=True)
        if 'recovery_pct_rank' in metrics_df.columns:
            metrics_df.rename(columns={'recovery_pct_rank': 'recovery_rank'}, inplace=True)
        if 'closing_strength_pct_rank' in metrics_df.columns:
            metrics_df.rename(columns={'closing_strength_pct_rank': 'closing_strength_rank'}, inplace=True)
        if 'last_hour_vol_ratio_rank' in metrics_df.columns:
            metrics_df.rename(columns={'last_hour_vol_ratio_rank': 'last_hour_vol_rank'}, inplace=True)
        if 'market_survivor_raw_rank' in metrics_df.columns:
            metrics_df.rename(columns={'market_survivor_raw_rank': 'market_survivor_rank'}, inplace=True)
        if 'breakout_quality_rank' in metrics_df.columns:
            metrics_df.rename(columns={'breakout_quality_rank': 'breakout_rank'}, inplace=True)

        # --- Final Score Calculation ---
        metrics_df['EMFB_Score'] = 0
        for metric, weight in weights.items():
            if metric in metrics_df.columns:
                metrics_df['EMFB_Score'] += metrics_df[metric].fillna(50) * (weight / 100.0)

        # --- Filtering and Final Touches ---
        final_df = metrics_df[metrics_df['EMFB_Score'] >= config.EMFB.MIN_SCORE_THRESHOLD].copy()
        if final_df.empty:
            return pd.DataFrame()

        final_df = final_df.sort_values('EMFB_Score', ascending=False).reset_index(drop=True)

        # --- Add Actionable Trading Levels and Explainability ---
        results: list[Dict[str, Any]] = []
        for _, row in final_df.iterrows():
            close = row['close']
            atr = row['atr']
            
            # Define stop loss based on ATR, but not higher than the session low
            atr_stop = close - (1.5 * atr)
            stop_loss_price = min(atr_stop, row['session_low'])
            
            target_price = close + ((close - stop_loss_price) * config.EMFB.RR_RATIO)

            # Build quantitative reason string
            reasons = [
                f"RS vs Nifty (1D): {row.get('rs_nifty_1d', 0):+.2%}",
                f"Recovery: {row.get('recovery_pct', 0):.0f}%",
                f"Closing Strength: {row.get('closing_strength_pct', 0):.0f}%",
                f"LH Vol Ratio: {row.get('last_hour_vol_ratio', 0):.1f}x",
                f"VWAP Dist: {row.get('vwap_distance_pct', 0):+.2f}%"
            ]
            
            score = row['EMFB_Score']
            confidence = "High" if score > 75 else ("Medium" if score > 60 else "Low")

            # --- Earnings Risk Annotation (see BaseScanner.scan's identical veto text) ---
            # Confidence stays a pure read of technical setup strength - earnings risk
            # is surfaced via Reason's prefix and the explicit Earnings_Risk field
            # instead, so downstream consumers can check Earnings_Risk directly rather
            # than string-matching Reason or misreading Confidence as risk-adjusted.
            earnings_risk = row.get('Earnings_Risk', '')
            reason_prefix = ""
            if "HIGH RISK" in earnings_risk:
                if "New/Recent Listing" in earnings_risk:
                    reason_prefix = "[🛑 VETO: New/recent listing with no earnings history - cannot verify earnings safety.] "
                else:
                    reason_prefix = f"[🛑 VETO: Earnings in {row.get('Days_To_Earnings', 0)} days. Avoid binary risk!] "
            elif "UNKNOWN" in earnings_risk:
                reason_prefix = "[❓ Earnings date unknown - verify manually before entry] "

            results.append({
                'Symbol': row['Symbol'],
                'Sector': row['Sector'],
                'EMFB_Score': round(score, 1),
                'Confidence': confidence,
                'Reason': reason_prefix + " | ".join(reasons),
                'Earnings_Date': row.get('Earnings_Date', 'Unknown'),
                'Days_To_Earnings': row.get('Days_To_Earnings', 999),
                'Earnings_Risk': earnings_risk,
                'Trigger': round(close, 2),
                'Stop': round(stop_loss_price, 2),
                'Target': round(target_price, 2),
                # Keep raw values for detailed reporting
                'RS_vs_Nifty': row.get('rs_nifty_1d_rank', 0),
                'RS_vs_Sector': row.get('rs_sector_1d_rank', 0),
                'Recovery': row.get('recovery_rank', 0),
                'Closing': row.get('closing_strength_rank', 0),
                'VWAP_Score': row.get('vwap_rank', 0),
                'Last_Hour_Vol': row.get('last_hour_vol_rank', 0),
                'Breakout_Score': row.get('breakout_rank', 0),
                # Raw RSI(14) - already computed in compute_daily_metrics and
                # percentile-ranked above ('rsi_rank'), but was never surfaced in
                # output or weighted into EMFB_Score: every WEIGHT_PROFILES entry in
                # config.py omits 'rsi_rank', so it was dead computation. Surfaced
                # here as an informational column only - deliberately NOT added to
                # any weight profile, since that would change EMFB_Score/ranking for
                # every existing consumer (stockscan, tier_performance win-rate
                # history, etc.) without the same backtested validation the current
                # weights have. Manual RSI checks throughout 2026-08 sessions kept
                # catching overbought entries (e.g. MOTILALOFS, BEL, AVALON) that
                # Extension_Flag's EMA20-distance measure missed - this closes that
                # gap for future sessions without touching scoring.
                'RSI_14': round(row.get('rsi', 50), 1),
                # Add lowercase aliases for backtester compatibility
                'strategy': 'EMFB',
                'timestamp': datetime.now(config.MARKET_TZ),
                'entry': round(close, 2),
                'stop': round(stop_loss_price, 2),
                'target': round(target_price, 2)
            })

        return pd.DataFrame(results)

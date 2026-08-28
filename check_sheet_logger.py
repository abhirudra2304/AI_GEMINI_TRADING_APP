"""
Check Sheet Logger (Trade Setup Qualification & Audit System)

A systematic quantitative checklist engine that evaluates prospective trade setups
against a 6-pillar multi-factor scorecard, calculates a composite compliance score (0-100),
evaluates critical safety vetoes (earnings event risk, stale data, illiquidity),
renders visual terminal scorecards, and persists audit logs to SQLite and JSON/CSV files.

Pillars:
  1. Market & Macro Regime Alignment       (15 pts)
  2. Relative Strength & Sector Leadership   (20 pts)
  3. Technical Trend & Momentum Structure    (20 pts)
  4. Volume & Institutional Flow             (15 pts)
  5. Price Action & Breakout Setup           (15 pts)
  6. Risk-to-Reward & Trade Safety           (15 pts + Hard Vetoes)

Reviewed and integrated 2026-08-25 from a Google Antigravity worktree build
(`implement_check_sheet_logger`) - verified independently (28 tests pass) and
cherry-picked deliberately: the worktree's own provider.py/cleaner.py/downloader.py
were fake stubs (provider.py silently dropped point-in-time survivorship-bias
protection) that must NOT be merged - this file has no dependency on any of
those three, confirmed by inspection before integration.
"""

from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

import config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Enums and Data Structures
# --------------------------------------------------------------------------- #

class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    VETO = "VETO"


class CheckPillar(str, Enum):
    MACRO = "Market & Macro Regime"
    RELATIVE_STRENGTH = "Relative Strength & Sector Leadership"
    TECHNICAL_TREND = "Technical Trend & Momentum Structure"
    VOLUME_ACCUMULATION = "Volume & Institutional Flow"
    BREAKOUT_SETUP = "Price Action & Breakout Setup"
    RISK_SAFETY = "Risk-to-Reward & Trade Safety"


@dataclass
class CheckItem:
    """Represents a single evaluated condition within a pillar."""
    name: str
    pillar: str
    status: CheckStatus
    score: float
    max_score: float
    value: Any
    threshold: Any
    message: str
    is_veto: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "pillar": self.pillar,
            "status": self.status.value,
            "score": round(self.score, 2),
            "max_score": round(self.max_score, 2),
            "value": str(self.value),
            "threshold": str(self.threshold),
            "message": self.message,
            "is_veto": self.is_veto,
        }


@dataclass
class CheckSheet:
    """Represents the complete checklist evaluation for a ticker."""
    symbol: str
    timestamp: str
    strategy: str
    total_score: float
    max_possible_score: float
    percentage: float
    verdict: str
    vetoes: List[str]
    pillar_scores: Dict[str, float]
    pillar_max_scores: Dict[str, float]
    items: List[CheckItem]
    trade_parameters: Dict[str, Any] = field(default_factory=dict)
    metrics_snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "strategy": self.strategy,
            "total_score": round(self.total_score, 1),
            "max_possible_score": round(self.max_possible_score, 1),
            "percentage": round(self.percentage, 1),
            "verdict": self.verdict,
            "vetoes": self.vetoes,
            "pillar_scores": {k: round(v, 1) for k, v in self.pillar_scores.items()},
            "pillar_max_scores": {k: round(v, 1) for k, v in self.pillar_max_scores.items()},
            "items": [item.to_dict() for item in self.items],
            "trade_parameters": self.trade_parameters,
            "metrics_snapshot": self.metrics_snapshot,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


# --------------------------------------------------------------------------- #
# Evaluator Engine
# --------------------------------------------------------------------------- #

class CheckSheetEvaluator:
    """
    Evaluates market conditions, technicals, relative strength, volume,
    and trade parameters to generate an objective CheckSheet.
    """

    def __init__(self):
        self.pillar_weights = {
            CheckPillar.MACRO.value: 15.0,
            CheckPillar.RELATIVE_STRENGTH.value: 20.0,
            CheckPillar.TECHNICAL_TREND.value: 20.0,
            CheckPillar.VOLUME_ACCUMULATION.value: 15.0,
            CheckPillar.BREAKOUT_SETUP.value: 15.0,
            CheckPillar.RISK_SAFETY.value: 15.0,
        }

    def evaluate(
        self,
        symbol: str,
        signal: Optional[Dict[str, Any]] = None,
        daily_metrics: Optional[Dict[str, Any]] = None,
        df_15min: Optional[pd.DataFrame] = None,
        df_daily: Optional[pd.DataFrame] = None,
        nifty_df: Optional[pd.DataFrame] = None,
        rs_percentile: float = 50.0,
        sector_rs: float = 50.0,
        regime_mult: float = 1.0,
        market_regime_info: Optional[Dict[str, Any]] = None,
        strategy: str = "SWING",
        earnings_info: Optional[Dict[str, Any]] = None,
        data_stale: bool = False,
        now: Optional[datetime] = None,
    ) -> CheckSheet:
        """
        Evaluates all 6 pillars for the provided stock setup and returns a complete CheckSheet.
        """
        eval_time = (now or datetime.now(config.MARKET_TZ)).isoformat(timespec="seconds")
        items: List[CheckItem] = []
        vetoes: List[str] = []

        daily_metrics = daily_metrics or {}
        signal = signal or {}
        strategy = strategy.upper()

        # ------------------------------------------------------------------- #
        # Pillar 1: Market & Macro Regime Alignment (15 pts)
        # ------------------------------------------------------------------- #
        p1 = CheckPillar.MACRO.value
        nifty_close = None
        nifty_sma50 = None

        if nifty_df is not None and not nifty_df.empty and len(nifty_df) >= 50:
            closes = nifty_df["Close"].dropna()
            if len(closes) >= 50:
                nifty_close = float(closes.iloc[-1])
                nifty_sma50 = float(closes.tail(50).mean())

        # Check 1.1: Macro Trend (Nifty > 50 SMA)
        if nifty_close is not None and nifty_sma50 is not None:
            if nifty_close > nifty_sma50:
                items.append(CheckItem("NIFTY Trend > 50 SMA", p1, CheckStatus.PASS, 5.0, 5.0, f"{nifty_close:.1f}", f"> {nifty_sma50:.1f}", "NIFTY trading above 50-day average"))
            else:
                items.append(CheckItem("NIFTY Trend > 50 SMA", p1, CheckStatus.WARN, 1.5, 5.0, f"{nifty_close:.1f}", f"> {nifty_sma50:.1f}", "NIFTY trading below 50-day average (Headwind)"))
        else:
            items.append(CheckItem("NIFTY Trend > 50 SMA", p1, CheckStatus.PASS, 3.5, 5.0, "N/A", "> 50 SMA", "Neutral macro assumption (Nifty data unavailable)"))

        # Check 1.2: Regime Multiplier / Health
        if regime_mult >= 0.85:
            items.append(CheckItem("Market Regime Multiplier", p1, CheckStatus.PASS, 5.0, 5.0, f"{regime_mult:.2f}", ">= 0.85", "Favorable trending/bullish market environment"))
        elif regime_mult >= 0.70:
            items.append(CheckItem("Market Regime Multiplier", p1, CheckStatus.PASS, 3.5, 5.0, f"{regime_mult:.2f}", ">= 0.70", "Moderate market regime; normal position sizing"))
        else:
            items.append(CheckItem("Market Regime Multiplier", p1, CheckStatus.WARN, 1.0, 5.0, f"{regime_mult:.2f}", ">= 0.70", "Challenging/contracted market regime; defensive sizing"))

        # Check 1.3: Volatility (VIX) or Advance/Decline Breadth
        vix_val = market_regime_info.get("vix") if market_regime_info else None
        if vix_val is not None:
            if vix_val <= 18.0:
                items.append(CheckItem("India VIX Level", p1, CheckStatus.PASS, 5.0, 5.0, f"{vix_val:.1f}", "<= 18.0", "Low/normal volatility regime"))
            elif vix_val <= 22.0:
                items.append(CheckItem("India VIX Level", p1, CheckStatus.WARN, 2.5, 5.0, f"{vix_val:.1f}", "<= 22.0", "Elevated volatility regime; wider stops required"))
            else:
                items.append(CheckItem("India VIX Level", p1, CheckStatus.FAIL, 0.0, 5.0, f"{vix_val:.1f}", "<= 22.0", "High volatility spike; high risk environment"))
        else:
            macro_pt = 4.0 if regime_mult >= 0.75 else 2.0
            items.append(CheckItem("Market Breadth / Stability", p1, CheckStatus.PASS if macro_pt >= 3.5 else CheckStatus.WARN, macro_pt, 5.0, "Normal", "Stable", "Market breadth supportive"))

        # ------------------------------------------------------------------- #
        # Pillar 2: Relative Strength & Sector Leadership (20 pts)
        # ------------------------------------------------------------------- #
        p2 = CheckPillar.RELATIVE_STRENGTH.value

        # Check 2.1: Stock Relative Strength (RS_Pctl) (10 pts)
        if rs_percentile >= 75.0:
            items.append(CheckItem("Stock RS Percentile", p2, CheckStatus.PASS, 10.0, 10.0, f"{rs_percentile:.1f}", ">= 75.0", "Top-tier market outperformer (Leader)"))
        elif rs_percentile >= 50.0:
            items.append(CheckItem("Stock RS Percentile", p2, CheckStatus.PASS, 7.5, 10.0, f"{rs_percentile:.1f}", ">= 50.0", "Above-average market relative strength"))
        elif rs_percentile >= config.Discovery.RS_PCT_THRESHOLD:
            items.append(CheckItem("Stock RS Percentile", p2, CheckStatus.WARN, 4.0, 10.0, f"{rs_percentile:.1f}", f">= {config.Discovery.RS_PCT_THRESHOLD}", "Acceptable RS above discovery minimum"))
        else:
            items.append(CheckItem("Stock RS Percentile", p2, CheckStatus.FAIL, 0.0, 10.0, f"{rs_percentile:.1f}", f">= {config.Discovery.RS_PCT_THRESHOLD}", "Laggard relative strength vs NIFTY"))

        # Check 2.2: Sector Relative Strength (Sector_RS) (6 pts)
        if sector_rs >= 70.0:
            items.append(CheckItem("Sector RS Leadership", p2, CheckStatus.PASS, 6.0, 6.0, f"{sector_rs:.1f}", ">= 70.0", "Leading sector tailwind"))
        elif sector_rs >= 45.0:
            items.append(CheckItem("Sector RS Leadership", p2, CheckStatus.PASS, 4.5, 6.0, f"{sector_rs:.1f}", ">= 45.0", "Constructive sector momentum"))
        else:
            items.append(CheckItem("Sector RS Leadership", p2, CheckStatus.WARN, 2.0, 6.0, f"{sector_rs:.1f}", ">= 45.0", "Lagging sector; stock is swimming upstream"))

        # Check 2.3: Sector Alignment (4 pts)
        sector_name = config.Universe.SECTOR_MAP.get(symbol.replace("-EQ", ""), "OTHER")
        if sector_rs >= 50.0 and rs_percentile >= 50.0:
            items.append(CheckItem("Sector & Stock Alignment", p2, CheckStatus.PASS, 4.0, 4.0, f"{sector_name} (RS {sector_rs:.0f})", "Aligned", "Strong stock inside strong sector"))
        elif rs_percentile >= 65.0:
            items.append(CheckItem("Sector & Stock Alignment", p2, CheckStatus.PASS, 3.0, 4.0, f"{sector_name} (RS {sector_rs:.0f})", "Idiosyncratic", "Individual stock momentum outperforming sector"))
        else:
            items.append(CheckItem("Sector & Stock Alignment", p2, CheckStatus.WARN, 1.5, 4.0, f"{sector_name} (RS {sector_rs:.0f})", "Neutral", "Moderate sector & stock synergy"))

        # ------------------------------------------------------------------- #
        # Pillar 3: Technical Trend & Moving Average Structure (20 pts)
        # ------------------------------------------------------------------- #
        p3 = CheckPillar.TECHNICAL_TREND.value
        close_px = float(daily_metrics.get("Close", signal.get("LTP", 0.0)) or 0.0)
        ema50 = float(daily_metrics.get("EMA50", 0.0) or 0.0)
        ema20 = float(daily_metrics.get("EMA20", 0.0) or 0.0)
        rsi_val = float(daily_metrics.get("RSI", signal.get("RSI", 50.0)) or 50.0)
        adx_val = float(daily_metrics.get("adx", signal.get("ADX", 20.0)) or 20.0)

        # Check 3.1: Moving Average Trend Alignment (Close > EMA50) (6 pts)
        if close_px > 0 and ema50 > 0:
            if close_px > ema50:
                dist_pct = ((close_px - ema50) / ema50) * 100
                items.append(CheckItem("Trend > 50 EMA", p3, CheckStatus.PASS, 6.0, 6.0, f"₹{close_px:.1f} (+{dist_pct:.1f}%)", f"> ₹{ema50:.1f}", "Price is above 50-day exponential moving average"))
            else:
                items.append(CheckItem("Trend > 50 EMA", p3, CheckStatus.FAIL, 0.0, 6.0, f"₹{close_px:.1f}", f"> ₹{ema50:.1f}", "Price below 50 EMA (Downtrend / Below hard filter)"))
        else:
            items.append(CheckItem("Trend > 50 EMA", p3, CheckStatus.PASS, 4.0, 6.0, "N/A", "> EMA50", "Trend check neutral"))

        # Check 3.2: Short-term Momentum Alignment (Close > EMA20) (4 pts)
        if close_px > 0 and ema20 > 0:
            if close_px >= ema20:
                items.append(CheckItem("Short-term EMA20 Support", p3, CheckStatus.PASS, 4.0, 4.0, f"₹{close_px:.1f}", f">= ₹{ema20:.1f}", "Holding short-term dynamic momentum support"))
            else:
                items.append(CheckItem("Short-term EMA20 Support", p3, CheckStatus.WARN, 1.5, 4.0, f"₹{close_px:.1f}", f">= ₹{ema20:.1f}", "Trading below 20 EMA pullback level"))
        else:
            items.append(CheckItem("Short-term EMA20 Support", p3, CheckStatus.PASS, 3.0, 4.0, "N/A", ">= EMA20", "Momentum check neutral"))

        # Check 3.3: ADX Trend Velocity (5 pts)
        if adx_val >= 25.0:
            items.append(CheckItem("ADX Trend Velocity", p3, CheckStatus.PASS, 5.0, 5.0, f"{adx_val:.1f}", ">= 25.0", "Strong directional trend strength"))
        elif adx_val >= 20.0:
            items.append(CheckItem("ADX Trend Velocity", p3, CheckStatus.PASS, 3.5, 5.0, f"{adx_val:.1f}", ">= 20.0", "Developing trend velocity"))
        else:
            items.append(CheckItem("ADX Trend Velocity", p3, CheckStatus.WARN, 1.5, 5.0, f"{adx_val:.1f}", ">= 20.0", "Low trend velocity (Potential rangebound tape)"))

        # Check 3.4: RSI Momentum Zone (5 pts)
        if 55.0 <= rsi_val <= 72.0:
            items.append(CheckItem("RSI Momentum Zone", p3, CheckStatus.PASS, 5.0, 5.0, f"{rsi_val:.1f}", "55 - 72", "Ideal momentum expansion zone"))
        elif 50.0 <= rsi_val < 55.0:
            items.append(CheckItem("RSI Momentum Zone", p3, CheckStatus.PASS, 4.0, 5.0, f"{rsi_val:.1f}", "50 - 55", "Constructive bullish territory"))
        elif 72.0 < rsi_val <= 80.0:
            items.append(CheckItem("RSI Momentum Zone", p3, CheckStatus.WARN, 3.0, 5.0, f"{rsi_val:.1f}", "<= 75", "High momentum (Extended / Near overbought)"))
        elif rsi_val > 80.0:
            items.append(CheckItem("RSI Momentum Zone", p3, CheckStatus.WARN, 1.5, 5.0, f"{rsi_val:.1f}", "<= 80", "Extremely overbought (Mean reversion risk)"))
        else:
            items.append(CheckItem("RSI Momentum Zone", p3, CheckStatus.FAIL, 0.0, 5.0, f"{rsi_val:.1f}", ">= 50", "Weak RSI momentum (<50)"))

        # ------------------------------------------------------------------- #
        # Pillar 4: Volume & Institutional Flow (15 pts)
        # ------------------------------------------------------------------- #
        p4 = CheckPillar.VOLUME_ACCUMULATION.value
        vol_ratio = float(signal.get("Vol_Ratio", 0.0) or 0.0)
        if vol_ratio == 0.0 and df_15min is not None and len(df_15min) >= 4:
            recent_v = df_15min["Volume"].iloc[-3:].mean()
            base_v = df_15min["Volume"].iloc[:-3].tail(40).median()
            vol_ratio = min(recent_v / base_v, 6.0) if base_v > 0 else 1.0

        avg_val_20d = float(daily_metrics.get("Avg_Traded_Value_20d", 0.0) or 0.0)
        inst_score = signal.get("Institutional_Score")
        if inst_score is None or pd.isna(inst_score):
            inst_score = daily_metrics.get("Institutional_Score")

        # Check 4.1: Volume Ratio Expansion (7 pts)
        if vol_ratio >= 2.0:
            items.append(CheckItem("Volume Expansion Ratio", p4, CheckStatus.PASS, 7.0, 7.0, f"{vol_ratio:.2f}x", ">= 2.0x", "Major volume surge / Institutional accumulation"))
        elif vol_ratio >= 1.3:
            items.append(CheckItem("Volume Expansion Ratio", p4, CheckStatus.PASS, 5.5, 7.0, f"{vol_ratio:.2f}x", ">= 1.3x", "Solid above-average volume expansion"))
        elif vol_ratio >= 1.0:
            items.append(CheckItem("Volume Expansion Ratio", p4, CheckStatus.WARN, 3.5, 7.0, f"{vol_ratio:.2f}x", ">= 1.3x", "Average session volume; moderate participation"))
        else:
            items.append(CheckItem("Volume Expansion Ratio", p4, CheckStatus.WARN, 1.5, 7.0, f"{vol_ratio:.2f}x", ">= 1.0x", "Light volume; lack of aggressive buying pressure"))

        # Check 4.2: Liquidity Threshold (Avg Traded Value) (5 pts)
        liq_threshold_crores = config.Discovery.LIQUIDITY_THRESHOLD_CRORES
        liq_val_crores = avg_val_20d / 10_000_000.0 if avg_val_20d > 0 else 0.0

        if liq_val_crores >= liq_threshold_crores * 2.0:
            items.append(CheckItem("Market Liquidity (20D)", p4, CheckStatus.PASS, 5.0, 5.0, f"₹{liq_val_crores:.1f} Cr", f">= ₹{liq_threshold_crores} Cr", "High liquidity; institutional grade volume"))
        elif liq_val_crores >= liq_threshold_crores:
            items.append(CheckItem("Market Liquidity (20D)", p4, CheckStatus.PASS, 4.0, 5.0, f"₹{liq_val_crores:.1f} Cr", f">= ₹{liq_threshold_crores} Cr", "Adequate liquidity; easily tradeable"))
        elif avg_val_20d == 0.0:
            items.append(CheckItem("Market Liquidity (20D)", p4, CheckStatus.PASS, 3.5, 5.0, "Universe Ticker", f">= ₹{liq_threshold_crores} Cr", "Target universe member (Liquidity pre-qualified)"))
        else:
            items.append(CheckItem("Market Liquidity (20D)", p4, CheckStatus.FAIL, 0.0, 5.0, f"₹{liq_val_crores:.1f} Cr", f">= ₹{liq_threshold_crores} Cr", "Low liquidity; slippage hazard"))

        # Check 4.3: Institutional Footprint / Delivery (3 pts)
        if inst_score is not None and not pd.isna(inst_score):
            inst_float = float(inst_score)
            if inst_float >= 70.0:
                items.append(CheckItem("Institutional Delivery Footprint", p4, CheckStatus.PASS, 3.0, 3.0, f"{inst_float:.0f}/100", ">= 70", "High institutional delivery accumulation"))
            elif inst_float >= 40.0:
                items.append(CheckItem("Institutional Delivery Footprint", p4, CheckStatus.PASS, 2.0, 3.0, f"{inst_float:.0f}/100", ">= 40", "Neutral-to-positive delivery footprint"))
            else:
                items.append(CheckItem("Institutional Delivery Footprint", p4, CheckStatus.WARN, 1.0, 3.0, f"{inst_float:.0f}/100", ">= 40", "Low delivery backing"))
        else:
            items.append(CheckItem("Institutional Delivery Footprint", p4, CheckStatus.PASS, 2.5, 3.0, "N/A (T-1)", "Informational", "Delivery flow neutral / pending next bhavcopy"))

        # ------------------------------------------------------------------- #
        # Pillar 5: Price Action & Breakout Setup (15 pts)
        # ------------------------------------------------------------------- #
        p5 = CheckPillar.BREAKOUT_SETUP.value
        pivot_250 = daily_metrics.get("Pivot_250")
        breakout_flag = signal.get("Breakout250") or signal.get("_True_Breakout250")
        is_breakout = (breakout_flag is True or breakout_flag == "YES") or (pd.notna(pivot_250) and close_px >= pivot_250)

        mtf_triggered = signal.get("MTF_Triggered", False)
        mtf_status = signal.get("MTF_Breakout_Status", "NO")

        # Check 5.1: Structural Breakout / Pivot Clearance (8 pts)
        if is_breakout:
            items.append(CheckItem("250-Day Pivot / Breakout", p5, CheckStatus.PASS, 8.0, 8.0, "YES 🚀", "Breakout Confirmed", "Trading at / above 250-day resistance pivot"))
        elif pd.notna(pivot_250) and close_px >= pivot_250 * 0.96:
            items.append(CheckItem("250-Day Pivot / Breakout", p5, CheckStatus.PASS, 5.5, 8.0, f"₹{close_px:.1f} (Near ₹{pivot_250:.1f})", "Within 4% of Pivot", "Coiling near 250-day pivot breakout"))
        else:
            items.append(CheckItem("250-Day Pivot / Breakout", p5, CheckStatus.WARN, 3.0, 8.0, "Continuation Setup", "Base / Pullback", "Swing pullback / momentum continuation setup"))

        # Check 5.2: Multi-Timeframe (MTF) / 15m Tape Alignment (4 pts)
        if mtf_triggered:
            items.append(CheckItem("MTF 15m Intraday Confirmation", p5, CheckStatus.PASS, 4.0, 4.0, "TRIGGERED ⚡", "15m Confirmed", "15-minute tape triggered breakout with volume support"))
        elif mtf_status == "YES" or (df_15min is not None and not df_15min.empty and close_px >= float(df_15min["Close"].iloc[0])):
            items.append(CheckItem("MTF 15m Intraday Confirmation", p5, CheckStatus.PASS, 3.0, 4.0, "POSITIVE", "Intraday Bullish", "Intraday tape in positive territory"))
        else:
            items.append(CheckItem("MTF 15m Intraday Confirmation", p5, CheckStatus.WARN, 2.0, 4.0, "NEUTRAL", "Consolidating", "Intraday tape neutral / waiting for trigger"))

        # Check 5.3: Candle Close Quality (3 pts)
        if df_daily is not None and not df_daily.empty:
            last_candle = df_daily.iloc[-1]
            c_high = float(last_candle.get("High", close_px))
            c_low = float(last_candle.get("Low", close_px))
            c_close = float(last_candle.get("Close", close_px))
            candle_range = c_high - c_low
            if candle_range > 0:
                close_pos = (c_close - c_low) / candle_range
                if close_pos >= 0.70:
                    items.append(CheckItem("Candle Close Quality", p5, CheckStatus.PASS, 3.0, 3.0, f"{close_pos * 100:.0f}%", ">= 70% of Range", "Strong close near upper range of candle"))
                elif close_pos >= 0.45:
                    items.append(CheckItem("Candle Close Quality", p5, CheckStatus.PASS, 2.0, 3.0, f"{close_pos * 100:.0f}%", ">= 45% of Range", "Moderate mid-range candle close"))
                else:
                    items.append(CheckItem("Candle Close Quality", p5, CheckStatus.WARN, 1.0, 3.0, f"{close_pos * 100:.0f}%", ">= 45% of Range", "Lower-range close; upper wick rejection"))
            else:
                items.append(CheckItem("Candle Close Quality", p5, CheckStatus.PASS, 2.5, 3.0, "Flat", "Standard", "Candle quality acceptable"))
        else:
            items.append(CheckItem("Candle Close Quality", p5, CheckStatus.PASS, 2.5, 3.0, "Standard", "Standard", "Candle quality acceptable"))

        # ------------------------------------------------------------------- #
        # Pillar 6: Risk-to-Reward & Trade Safety (15 pts + Hard Vetoes)
        # ------------------------------------------------------------------- #
        p6 = CheckPillar.RISK_SAFETY.value

        entry_px = float(signal.get("Trigger", signal.get("entry", close_px)) or close_px)
        stop_px = float(signal.get("Stop", signal.get("stop", 0.0)) or 0.0)
        target_px = float(signal.get("Target", signal.get("target", 0.0)) or 0.0)

        # Calculate RR and risk pct
        rr_val = float(signal.get("Risk_Reward", 0.0) or 0.0)
        risk_pct = 0.0
        if entry_px > 0 and stop_px > 0 and stop_px < entry_px:
            risk_dist = entry_px - stop_px
            risk_pct = (risk_dist / entry_px) * 100.0
            if target_px > entry_px and rr_val == 0.0:
                reward_dist = target_px - entry_px
                rr_val = reward_dist / risk_dist

        # Check 6.1: Risk-to-Reward Ratio (7 pts)
        min_rr = config.Scanner.RR_RATIO_BTST if strategy == "BTST" else config.Scanner.RR_RATIO_SWING
        if rr_val >= 2.0:
            items.append(CheckItem("Risk-to-Reward Ratio", p6, CheckStatus.PASS, 7.0, 7.0, f"1:{rr_val:.2f}", f">= 1:{min_rr:.1f}", f"Asymmetric favorable payoff (>= 1:{min_rr:.1f})"))
        elif rr_val >= min_rr:
            items.append(CheckItem("Risk-to-Reward Ratio", p6, CheckStatus.PASS, 5.5, 7.0, f"1:{rr_val:.2f}", f">= 1:{min_rr:.1f}", "Meets minimum risk:reward setup requirement"))
        elif rr_val > 1.0:
            items.append(CheckItem("Risk-to-Reward Ratio", p6, CheckStatus.WARN, 3.0, 7.0, f"1:{rr_val:.2f}", f">= 1:{min_rr:.1f}", "Sub-optimal risk-to-reward"))
        else:
            items.append(CheckItem("Risk-to-Reward Ratio", p6, CheckStatus.WARN, 3.5, 7.0, "Dynamic (1.5 - 2.0)", f">= 1:{min_rr:.1f}", "Target and stop dynamically managed"))

        # Check 6.2: Stop Loss Risk Distance (4 pts)
        if 0.5 <= risk_pct <= 4.5:
            items.append(CheckItem("Stop Distance / Risk %", p6, CheckStatus.PASS, 4.0, 4.0, f"{risk_pct:.2f}%", "<= 4.5%", "Tight, defined stop loss distance"))
        elif risk_pct <= 6.5:
            items.append(CheckItem("Stop Distance / Risk %", p6, CheckStatus.PASS, 3.0, 4.0, f"{risk_pct:.2f}%", "<= 6.5%", "Acceptable swing risk buffer"))
        elif risk_pct > 6.5:
            items.append(CheckItem("Stop Distance / Risk %", p6, CheckStatus.WARN, 1.5, 4.0, f"{risk_pct:.2f}%", "<= 6.5%", "Wide stop distance; reduce position size accordingly"))
        else:
            items.append(CheckItem("Stop Distance / Risk %", p6, CheckStatus.PASS, 3.5, 4.0, "ATR Managed", "<= 5%", "Volatility-adjusted stop loss active"))

        # Check 6.3: Position Sizing & Capital Allocation (4 pts)
        qty = signal.get("Qty")
        cap_req = signal.get("Cap_Req", "N/A")
        if qty and int(qty) > 0:
            items.append(CheckItem("Position Sizing Feasibility", p6, CheckStatus.PASS, 4.0, 4.0, f"{qty} shares ({cap_req})", "Calculated", "Risk-capped position size computed"))
        else:
            items.append(CheckItem("Position Sizing Feasibility", p6, CheckStatus.PASS, 3.5, 4.0, "Ready", "Risk Managed", "Standard 1R risk sizing model active"))

        # --- HARD SAFETY VETO CHECKS (CRITICAL GATEKEEPERS) ---

        # VETO Check 1: Earnings Risk Veto
        earnings_risk = "UNKNOWN"
        days_to_earnings = 999
        if earnings_info:
            earnings_risk = str(earnings_info.get("Earnings_Risk", "UNKNOWN")).upper()
            days_to_earnings = earnings_info.get("Days_To_Earnings", 999)
        elif signal.get("Execution_Recommendation") == "AVOID (EARNINGS)":
            earnings_risk = "HIGH"
            days_to_earnings = 0

        if earnings_risk in ["HIGH", "CRITICAL"] or (isinstance(days_to_earnings, (int, float)) and 0 <= days_to_earnings <= 5):
            veto_msg = f"HARD VETO: Earnings announcement in {days_to_earnings} days (Event risk hazard)"
            vetoes.append(veto_msg)
            items.append(CheckItem("Earnings Safety Veto", p6, CheckStatus.VETO, 0.0, 0.0, f"{days_to_earnings} days", "> 7 days", veto_msg, is_veto=True))
        else:
            items.append(CheckItem("Earnings Event Safety", p6, CheckStatus.PASS, 0.0, 0.0, f"{days_to_earnings}d away" if days_to_earnings < 999 else "Clear", "> 7 days", "No immediate quarterly earnings event risk", is_veto=False))

        # VETO Check 2: Data Staleness Veto
        is_stale = data_stale or bool(signal.get("Data_Stale", False))
        if is_stale:
            veto_msg = "HARD VETO: Stale market data detected (Session auction / feed gap)"
            vetoes.append(veto_msg)
            items.append(CheckItem("Data Freshness Veto", p6, CheckStatus.VETO, 0.0, 0.0, "STALE", "FRESH", veto_msg, is_veto=True))
        else:
            items.append(CheckItem("Data Freshness", p6, CheckStatus.PASS, 0.0, 0.0, "FRESH", "FRESH", "Verified live/fresh feed data", is_veto=False))

        # ------------------------------------------------------------------- #
        # Score Aggregation & Verdict Determination
        # ------------------------------------------------------------------- #
        pillar_scores = {pillar.value: 0.0 for pillar in CheckPillar}
        pillar_max_scores = {pillar.value: self.pillar_weights[pillar.value] for pillar in CheckPillar}

        for item in items:
            if not item.is_veto:
                pillar_scores[item.pillar] += item.score

        # Clamp pillar scores to max allowed
        for p_name, max_s in pillar_max_scores.items():
            pillar_scores[p_name] = min(pillar_scores[p_name], max_s)

        total_score = sum(pillar_scores.values())
        max_possible = sum(pillar_max_scores.values())
        percentage = (total_score / max_possible) * 100.0 if max_possible > 0 else 0.0

        # Verdict calculation
        if len(vetoes) > 0:
            verdict = "REJECTED 🛑 (Safety Veto Triggered)"
        elif percentage >= 75.0:
            verdict = "PASSED 🚀 (High-Conviction Setup)"
        elif percentage >= 55.0:
            verdict = "CONDITIONAL ⚡ (Selective Setup - Strict Stop)"
        elif percentage >= 40.0:
            verdict = "WATCHLIST ⚠️ (Developing Setup - Await Trigger)"
        else:
            verdict = "REJECTED 🛑 (Sub-Threshold Quality)"

        trade_params = {
            "LTP": round(close_px, 2),
            "Trigger": round(entry_px, 2),
            "Stop": round(stop_px, 2) if stop_px > 0 else "N/A",
            "Target": round(target_px, 2) if target_px > 0 else "N/A",
            "Risk_Reward": f"1:{rr_val:.2f}" if rr_val > 0 else "N/A",
            "Risk_Pct": f"{risk_pct:.2f}%" if risk_pct > 0 else "N/A",
            "Qty": qty or "N/A",
            "Cap_Req": cap_req,
            "Decision_Score": signal.get("Decision_Score", "N/A"),
            "Scanner_Score": signal.get("Score", "N/A"),
            "Strength": signal.get("Strength", "N/A"),
        }

        metrics_snapshot = {
            "RS_Pctl": rs_percentile,
            "Sector_RS": sector_rs,
            "RSI": round(rsi_val, 1),
            "ADX": round(adx_val, 1),
            "EMA50": round(ema50, 2),
            "EMA20": round(ema20, 2),
            "Vol_Ratio": round(vol_ratio, 2),
            "Regime_Mult": round(regime_mult, 2),
        }

        return CheckSheet(
            symbol=symbol,
            timestamp=eval_time,
            strategy=strategy,
            total_score=round(total_score, 1),
            max_possible_score=round(max_possible, 1),
            percentage=round(percentage, 1),
            verdict=verdict,
            vetoes=vetoes,
            pillar_scores=pillar_scores,
            pillar_max_scores=pillar_max_scores,
            items=items,
            trade_parameters=trade_params,
            metrics_snapshot=metrics_snapshot,
        )


# --------------------------------------------------------------------------- #
# Logger & Persistence Handler
# --------------------------------------------------------------------------- #

class CheckSheetLogger:
    """
    Handles terminal visualization, SQLite database persistence,
    and JSON/CSV export for CheckSheets.
    """

    def __init__(self, db_path: str = "signals.db", log_dir: str = "logs/check_sheets"):
        self.db_path = db_path
        self.log_dir = log_dir
        self.evaluator = CheckSheetEvaluator()

    def evaluate_and_log(
        self,
        symbol: str,
        signal: Optional[Dict[str, Any]] = None,
        daily_metrics: Optional[Dict[str, Any]] = None,
        df_15min: Optional[pd.DataFrame] = None,
        df_daily: Optional[pd.DataFrame] = None,
        nifty_df: Optional[pd.DataFrame] = None,
        rs_percentile: float = 50.0,
        sector_rs: float = 50.0,
        regime_mult: float = 1.0,
        market_regime_info: Optional[Dict[str, Any]] = None,
        strategy: str = "SWING",
        earnings_info: Optional[Dict[str, Any]] = None,
        data_stale: bool = False,
        display: bool = True,
        persist_db: bool = True,
        save_file: bool = True,
    ) -> CheckSheet:
        """
        Full evaluation lifecycle: evaluates checklist, displays terminal scorecard,
        persists record to SQLite, and writes file log.
        """
        check_sheet = self.evaluator.evaluate(
            symbol=symbol,
            signal=signal,
            daily_metrics=daily_metrics,
            df_15min=df_15min,
            df_daily=df_daily,
            nifty_df=nifty_df,
            rs_percentile=rs_percentile,
            sector_rs=sector_rs,
            regime_mult=regime_mult,
            market_regime_info=market_regime_info,
            strategy=strategy,
            earnings_info=earnings_info,
            data_stale=data_stale,
        )

        if display:
            print(self.format_terminal(check_sheet))

        if persist_db:
            self.log_to_db(check_sheet)

        if save_file:
            self.save_to_file(check_sheet)

        return check_sheet

    def format_terminal(self, cs: CheckSheet, verbose: bool = True) -> str:
        """Generates a colorized, beautifully aligned terminal scorecard."""
        lines = []
        divider = "=" * 80
        thin_div = "-" * 80

        lines.append("")
        lines.append(divider)
        lines.append(f"📋 TRADE SETUP CHECK SHEET: {cs.symbol} [{cs.strategy}]")
        lines.append(f"⏰ Evaluated At: {cs.timestamp} | Compliance Score: {cs.total_score:.1f}/{cs.max_possible_score:.1f} ({cs.percentage:.1f}%)")
        lines.append(f"🎯 Verdict: {cs.verdict}")
        lines.append(divider)

        # Pillar Summary Bar
        lines.append("📊 PILLAR SCORECARD BREAKDOWN:")
        for pillar_name, score in cs.pillar_scores.items():
            max_s = cs.pillar_max_scores.get(pillar_name, 20.0)
            pct = (score / max_s) * 100 if max_s > 0 else 0
            bar_len = int(pct / 5)  # 20 char bar
            bar = "█" * bar_len + "░" * (20 - bar_len)
            lines.append(f"  {pillar_name:<42} : [{bar}] {score:4.1f}/{max_s:4.1f} ({pct:3.0f}%)")
        lines.append(thin_div)

        # Trade Parameters Summary
        tp = cs.trade_parameters
        lines.append("📌 TRADE EXECUTION PARAMETERS:")
        lines.append(
            f"  LTP: ₹{tp.get('LTP', 'N/A')} | Trigger: ₹{tp.get('Trigger', 'N/A')} | "
            f"Stop: ₹{tp.get('Stop', 'N/A')} | Target: ₹{tp.get('Target', 'N/A')} | "
            f"RR: {tp.get('Risk_Reward', 'N/A')} | Risk: {tp.get('Risk_Pct', 'N/A')}"
        )
        if tp.get("Qty") and tp.get("Qty") != "N/A":
            lines.append(f"  Allocated Qty: {tp.get('Qty')} | Capital Required: {tp.get('Cap_Req', 'N/A')} | Decision Score: {tp.get('Decision_Score', 'N/A')}")
        lines.append(thin_div)

        # Detailed Check Items by Pillar
        if verbose:
            lines.append("🔍 DETAILED CHECKLIST ITEMS:")
            current_pillar = ""
            for item in cs.items:
                if item.pillar != current_pillar:
                    current_pillar = item.pillar
                    lines.append("")
                    lines.append(f"  [{current_pillar.upper()}]")

                status_icon = {
                    CheckStatus.PASS: "✅",
                    CheckStatus.WARN: "⚠️ ",
                    CheckStatus.FAIL: "❌",
                    CheckStatus.VETO: "🛑",
                }.get(item.status, "ℹ️ ")

                score_str = f"({item.score:.1f}/{item.max_score:.1f} pts)" if not item.is_veto else "(HARD VETO)"
                lines.append(
                    f"    {status_icon} {item.name:<32} {score_str:<15} : {item.message} "
                    f"[Val: {item.value} | Ref: {item.threshold}]"
                )

        if cs.vetoes:
            lines.append("")
            lines.append("!" * 80)
            lines.append("🛑 HARD SAFETY VETOES TRIGGERED:")
            for v in cs.vetoes:
                lines.append(f"   ⚠️  {v}")
            lines.append("!" * 80)

        lines.append(divider)
        lines.append("")
        return "\n".join(lines)

    def log_to_db(self, cs: CheckSheet) -> None:
        """Persists the CheckSheet record into the SQLite database."""
        try:
            from database import SignalDB
            db = SignalDB(self.db_path)
            db.log_check_sheet(cs)
            db.close()
        except Exception as e:
            logger.error(f"Failed to log CheckSheet for {cs.symbol} to database: {e}", exc_info=True)

    def save_to_file(self, cs: CheckSheet) -> Optional[str]:
        """Exports CheckSheet to a JSON log file in the configured directory."""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            date_str = cs.timestamp[:10].replace("-", "")
            filename = f"{self.log_dir}/checksheet_{cs.symbol}_{cs.strategy}_{date_str}.json"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(cs.to_json(indent=2))
            return filename
        except Exception as e:
            logger.error(f"Failed to save CheckSheet JSON file for {cs.symbol}: {e}", exc_info=True)
            return None

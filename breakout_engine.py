"""
Breakout Pattern Recognition Engine (IAS Plug-in Module)

Vectorized detection of Minervini-style technical setups — Trend Template
(Stage 2), Volatility Contraction Pattern (VCP), Flat Base, Cup & Handle,
High Tight Flag, and Pocket Pivot — across a universe of stocks, using only
Price, Volume and Moving Averages (no lagging oscillators such as RSI or
MACD).

Design notes
------------
- Completely independent: no imports from config.py, data_broker.py,
  scanner_engine.py or any other existing module, and nothing in the
  existing codebase imports this one. Adding it carries zero regression
  risk to Version 1.0.
- All lookback windows, percentage thresholds and scoring weights live in
  `BreakoutConfig`, a dataclass passed into the engine — nothing is
  hardcoded inline.
- No global/module-level mutable state. All state lives on an instance of
  BreakoutEngine (its wide OHLCV frames and `config`), passed explicitly
  by the caller.
- Calculation methods operate on whole per-field DataFrames
  (DatetimeIndex x tickers) via vectorized Pandas/NumPy ops (`.rolling`,
  `.shift`, elementwise comparison) — they never iterate over rows. The
  only per-ticker loops in the module are (a) reshaping the input dict of
  per-ticker OHLCV frames into wide per-field frames in `__init__`, and
  (b) packaging each column's latest values into its own
  BreakoutAnalysis in `generate_analysis()` — both are unavoidable I/O
  shape transforms, not part of the pattern math itself.
- Pattern definitions that Minervini's own methodology treats as visual /
  fuzzy (exact swing-high/swing-low identification, precise handle
  geometry) are implemented here as clearly-documented vectorizable
  proxies rather than approximated with row-by-row peak-finding, which
  would break the vectorization constraint. Each proxy is called out in
  its method docstring.
- Logs at DEBUG for input shape and INFO for the final analysis summary
  (patterns detected / trend-template pass counts), via a module-level
  `logger` (standard `logging` module).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Output dataclass
# --------------------------------------------------------------------------- #

@dataclass
class BreakoutAnalysis:
    """Latest breakout-pattern reading for a single ticker.

    Attributes:
        ticker: Stock ticker.
        trend_template_pass: Minervini Stage 2 Trend Template result. If
            False, `detected_patterns` is empty and `confidence` is 0.0.
        detected_patterns: Names of patterns detected on the latest day,
            e.g. ['VCP', 'Pocket Pivot']. Always empty when
            `trend_template_pass` is False.
        base_quality: 0-100, higher when ATR has been shrinking (tighter
            price action).
        contraction_score: 0-100, higher when the base's right side is
            tighter than its left side.
        volume_dry_up: 0-100, higher when recent volume is low relative
            to its 50-day average.
        pivot_quality: 0-100, higher when today's close is near the day's
            high.
        risk_reward_pct: % risk from today's close to the stop-loss (the
            trailing lookback-day low), e.g. 5.4 means 5.4% downside to
            the stop.
        confidence: 0-100 weighted composite of base_quality,
            contraction_score, volume_dry_up and pivot_quality. Forced to
            0.0 when `trend_template_pass` is False.
    """
    ticker: str
    trend_template_pass: bool
    detected_patterns: List[str]
    base_quality: float
    contraction_score: float
    volume_dry_up: float
    pivot_quality: float
    risk_reward_pct: float
    confidence: float


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BreakoutConfig:
    """All tunable windows, thresholds and weights for BreakoutEngine.

    Attributes:
        sma_short, sma_mid, sma_long: SMA periods for the Trend Template
            (Close > sma_short > sma_mid > sma_long).
        sma_long_trend_lookback: Sessions back over which sma_long must
            have net-risen for Trend Template Condition B.
        week52_lookback: Sessions approximating a 52-week lookback for
            Trend Template Condition C.
        near_52w_high_pct: Max % below the 52-week high the current price
            may sit at and still pass Condition C.
        htf_rally_lookback: Window (sessions) searched for a High Tight
            Flag qualifying rally.
        htf_min_rally_pct: Minimum % swing within `htf_rally_lookback`
            required to qualify as the HTF rally leg.
        htf_flag_max_depth_pct: Max % pullback from the local high allowed
            during the HTF flag/consolidation.
        htf_flag_min_days: Minimum sessions the flag must have persisted
            (approximated as: the local high must be older than this many
            sessions).
        htf_flag_max_days: Window (sessions) the flag's local high/depth
            is measured over.
        flat_base_window: Sessions over which Flat Base range depth is
            measured.
        flat_base_max_depth_pct: Max % range depth (HH to LL) allowed for
            a Flat Base.
        pocket_pivot_lookback: Trailing sessions (excluding today) whose
            down-day volume today's up-day volume must exceed.
        vcp_base_window: Sessions defining the overall VCP base ("left
            side") for depth and contraction-count purposes.
        vcp_max_base_depth_pct: Max % depth of the overall VCP base.
        vcp_recent_window: Sessions defining the most recent ("right
            side") contraction leg.
        vcp_recent_max_range_pct: Max % range depth allowed for the most
            recent contraction leg.
        vcp_preceding_window: Sessions defining the contraction leg
            immediately preceding the most recent one, used to confirm
            the range is tightening leg-over-leg.
        cup_lookback: Sessions defining the Cup & Handle cup portion
            (excluding the handle).
        cup_min_depth_pct, cup_max_depth_pct: Depth band (% off the cup's
            left-side high) a valid cup must fall within.
        cup_right_side_tolerance_pct: Max % the current price may sit
            below the cup's left-side high for the right side to count as
            "recovered".
        handle_lookback: Sessions defining the handle portion.
        handle_max_depth_pct: Max % range depth allowed within the handle.
        volume_dryup_short_window, volume_dryup_long_window: Sessions for
            the short/long volume averages compared in Volume Dry-up.
        atr_window: Sessions used to average True Range for Base Quality.
        base_quality_lookback: Sessions back ATR is compared against to
            measure shrinkage for Base Quality.
        stop_loss_lookback_days: Sessions used to find the stop-loss low
            for Risk/Reward.
        weight_base_quality, weight_contraction, weight_volume_dryup,
            weight_pivot_quality: Confidence composite weights.
    """
    sma_short: int = 50
    sma_mid: int = 150
    sma_long: int = 200
    sma_long_trend_lookback: int = 20
    week52_lookback: int = 252
    near_52w_high_pct: float = 25.0

    htf_rally_lookback: int = 40
    htf_min_rally_pct: float = 100.0
    htf_flag_max_depth_pct: float = 25.0
    htf_flag_min_days: int = 15
    htf_flag_max_days: int = 25

    flat_base_window: int = 20
    flat_base_max_depth_pct: float = 15.0

    pocket_pivot_lookback: int = 10

    vcp_base_window: int = 50
    vcp_max_base_depth_pct: float = 35.0
    vcp_recent_window: int = 10
    vcp_recent_max_range_pct: float = 10.0
    vcp_preceding_window: int = 20

    cup_lookback: int = 65
    cup_min_depth_pct: float = 12.0
    cup_max_depth_pct: float = 33.0
    cup_right_side_tolerance_pct: float = 10.0
    handle_lookback: int = 10
    handle_max_depth_pct: float = 12.0

    volume_dryup_short_window: int = 5
    volume_dryup_long_window: int = 50
    atr_window: int = 14
    base_quality_lookback: int = 15
    stop_loss_lookback_days: int = 20

    weight_base_quality: float = 25.0
    weight_contraction: float = 25.0
    weight_volume_dryup: float = 25.0
    weight_pivot_quality: float = 25.0

    @property
    def confidence_weights_sum(self) -> float:
        return (
            self.weight_base_quality
            + self.weight_contraction
            + self.weight_volume_dryup
            + self.weight_pivot_quality
        )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class BreakoutEngine:
    """Vectorized calculator that turns per-ticker OHLCV data into a
    per-ticker dict of BreakoutAnalysis.

    Example:
        engine = BreakoutEngine(data, BreakoutConfig())
        analysis = engine.generate_analysis()
        analysis['HAL.NS'].detected_patterns
    """

    def __init__(self, data: Dict[str, pd.DataFrame], config: Optional[BreakoutConfig] = None):
        """
        Args:
            data: Dict[str, pd.DataFrame] mapping ticker -> OHLCV
                DataFrame (columns Open, High, Low, Close, Volume;
                DatetimeIndex, sorted ascending).
            config: Windows/thresholds/weights to use. Defaults to
                BreakoutConfig() if not supplied.
        """
        self.config = config or BreakoutConfig()
        self.open, self.high, self.low, self.close, self.volume = self._normalize_input(data)

    # -- input normalization (shape transform, not math) ------------------- #

    def _normalize_input(self, data: Dict[str, pd.DataFrame]):
        """Reshapes the input dict into five wide per-field DataFrames
        (DatetimeIndex x tickers): Open, High, Low, Close, Volume. This is
        a structural transform, not a calculation."""
        tickers = list(data.keys())
        open_ = pd.concat({t: data[t]["Open"] for t in tickers}, axis=1)
        high = pd.concat({t: data[t]["High"] for t in tickers}, axis=1)
        low = pd.concat({t: data[t]["Low"] for t in tickers}, axis=1)
        close = pd.concat({t: data[t]["Close"] for t in tickers}, axis=1)
        volume = pd.concat({t: data[t]["Volume"] for t in tickers}, axis=1)
        return (
            open_.sort_index(),
            high.sort_index(),
            low.sort_index(),
            close.sort_index(),
            volume.sort_index(),
        )

    @staticmethod
    def _safe_divide(numerator: pd.DataFrame, denominator: pd.DataFrame) -> pd.DataFrame:
        """Elementwise division that yields NaN instead of inf where the
        denominator is zero or missing."""
        safe_denominator = denominator.where(denominator != 0, other=np.nan)
        return numerator / safe_denominator

    # -- Trend Template ------------------------------------------------------ #

    def calculate_trend_template(self) -> pd.DataFrame:
        """Minervini Stage 2 Trend Template, evaluated at every date for
        every ticker:
            A) Close > SMA(sma_short) > SMA(sma_mid) > SMA(sma_long)
            B) SMA(sma_long) has net-risen over the last
               `sma_long_trend_lookback` sessions
            C) Close is within `near_52w_high_pct` of the trailing
               `week52_lookback`-session high

        Returns:
            Boolean DataFrame aligned to `self.close`.
        """
        cfg = self.config
        sma_short = self.close.rolling(cfg.sma_short).mean()
        sma_mid = self.close.rolling(cfg.sma_mid).mean()
        sma_long = self.close.rolling(cfg.sma_long).mean()

        condition_a = (self.close > sma_short) & (sma_short > sma_mid) & (sma_mid > sma_long)
        condition_b = sma_long > sma_long.shift(cfg.sma_long_trend_lookback)

        week52_high = self.close.rolling(cfg.week52_lookback, min_periods=1).max()
        pct_below_high = self._safe_divide(week52_high - self.close, week52_high) * 100.0
        condition_c = pct_below_high <= cfg.near_52w_high_pct

        return condition_a & condition_b & condition_c

    # -- Pattern flags ------------------------------------------------------- #

    def calculate_high_tight_flag(self) -> pd.DataFrame:
        """High Tight Flag: a >= `htf_min_rally_pct` swing within the
        trailing `htf_rally_lookback` sessions, followed by a
        consolidation that is shallow (<= `htf_flag_max_depth_pct` off the
        local high) and old enough to imply it has lasted at least
        `htf_flag_min_days` sessions.

        Proxy note: exact rally-leg / flag-leg segmentation requires
        swing-point detection, which isn't vectorizable without row
        iteration. This method instead checks (1) the max/min swing within
        the rally window, (2) the current depth off the local high within
        `htf_flag_max_days`, and (3) that the local high itself falls
        outside the most recent `htf_flag_min_days` sessions (i.e. price
        hasn't made a fresh high recently) as a stand-in for "consolidation
        has persisted at least htf_flag_min_days".

        Returns:
            Boolean DataFrame aligned to `self.close`.
        """
        cfg = self.config
        rally_high = self.close.rolling(cfg.htf_rally_lookback).max()
        rally_low = self.close.rolling(cfg.htf_rally_lookback).min()
        rally_pct = self._safe_divide(rally_high, rally_low).sub(1.0) * 100.0
        rally_ok = rally_pct >= cfg.htf_min_rally_pct

        flag_high = self.close.rolling(cfg.htf_flag_max_days).max()
        flag_depth_pct = self._safe_divide(flag_high - self.close, flag_high) * 100.0
        flag_depth_ok = flag_depth_pct <= cfg.htf_flag_max_depth_pct

        recent_high = self.close.rolling(cfg.htf_flag_min_days).max()
        duration_ok = recent_high < flag_high

        return rally_ok & flag_depth_ok & duration_ok

    def calculate_flat_base(self) -> pd.DataFrame:
        """Flat Base: rolling `flat_base_window`-session High-to-Low depth
        <= `flat_base_max_depth_pct`.

        Returns:
            Boolean DataFrame aligned to `self.close`.
        """
        cfg = self.config
        window_high = self.high.rolling(cfg.flat_base_window).max()
        window_low = self.low.rolling(cfg.flat_base_window).min()
        depth_pct = self._safe_divide(window_high - window_low, window_high) * 100.0
        return depth_pct <= cfg.flat_base_max_depth_pct

    def calculate_pocket_pivot(self) -> pd.DataFrame:
        """Pocket Pivot: today's close > previous close AND today's
        volume > the highest down-day volume over the trailing
        `pocket_pivot_lookback` sessions (excluding today).

        Returns:
            Boolean DataFrame aligned to `self.close`.
        """
        cfg = self.config
        prev_close = self.close.shift(1)
        up_day = self.close > prev_close

        is_down_day = self.close < prev_close
        down_day_volume = self.volume.where(is_down_day, other=np.nan)
        max_down_volume = down_day_volume.shift(1).rolling(cfg.pocket_pivot_lookback).max()

        return up_day & (self.volume > max_down_volume)

    def calculate_vcp(self) -> pd.DataFrame:
        """Volatility Contraction Pattern: overall base depth
        <= `vcp_max_base_depth_pct`, with at least two sequential
        contractions — the most recent `vcp_recent_window`-session range
        (<= `vcp_recent_max_range_pct`) tighter than the
        `vcp_preceding_window`-session range before it, which in turn must
        be tighter than the window before that.

        Returns:
            Boolean DataFrame aligned to `self.close`.
        """
        cfg = self.config
        base_depth = self._left_base_depth()
        base_depth_ok = base_depth <= cfg.vcp_max_base_depth_pct

        recent_range = self._range_pct(self.high, self.low, cfg.vcp_recent_window, shift_by=0)
        recent_tight = recent_range <= cfg.vcp_recent_max_range_pct

        preceding_range = self._range_pct(
            self.high, self.low, cfg.vcp_preceding_window, shift_by=cfg.vcp_recent_window
        )
        first_contraction = recent_range < preceding_range

        prior_range = self._range_pct(
            self.high,
            self.low,
            cfg.vcp_preceding_window,
            shift_by=cfg.vcp_recent_window + cfg.vcp_preceding_window,
        )
        second_contraction = preceding_range < prior_range

        return base_depth_ok & recent_tight & first_contraction & second_contraction

    def calculate_cup_and_handle(self) -> pd.DataFrame:
        """Cup & Handle (vectorizable proxy — the spec's Pattern
        Identification section formalizes High Tight Flag, Flat Base,
        Pocket Pivot and VCP but not Cup & Handle; this method extends the
        same rolling-window approach to it):
            - Cup: the `cup_lookback`-session range (excluding the handle)
              has depth between `cup_min_depth_pct` and `cup_max_depth_pct`
              off its high, and price has recovered to within
              `cup_right_side_tolerance_pct` of that high.
            - Handle: the trailing `handle_lookback`-session range depth
              is shallow (<= `handle_max_depth_pct`).

        Returns:
            Boolean DataFrame aligned to `self.close`.
        """
        cfg = self.config
        cup_high = self.close.shift(cfg.handle_lookback).rolling(cfg.cup_lookback).max()
        cup_low = self.close.shift(cfg.handle_lookback).rolling(cfg.cup_lookback).min()
        cup_depth_pct = self._safe_divide(cup_high - cup_low, cup_high) * 100.0
        cup_depth_ok = (cup_depth_pct >= cfg.cup_min_depth_pct) & (cup_depth_pct <= cfg.cup_max_depth_pct)

        right_side_off_high_pct = self._safe_divide(cup_high - self.close, cup_high) * 100.0
        right_side_recovered = right_side_off_high_pct <= cfg.cup_right_side_tolerance_pct

        handle_high = self.close.rolling(cfg.handle_lookback).max()
        handle_low = self.close.rolling(cfg.handle_lookback).min()
        handle_depth_pct = self._safe_divide(handle_high - handle_low, handle_high) * 100.0
        handle_ok = handle_depth_pct <= cfg.handle_max_depth_pct

        return cup_depth_ok & right_side_recovered & handle_ok

    # -- shared helpers for VCP / scoring ------------------------------------ #

    def _range_pct(
        self, high: pd.DataFrame, low: pd.DataFrame, window: int, shift_by: int
    ) -> pd.DataFrame:
        """% range depth (rolling High - rolling Low) / rolling High over
        `window` sessions, evaluated `shift_by` sessions back from today."""
        shifted_high = high.shift(shift_by)
        shifted_low = low.shift(shift_by)
        window_high = shifted_high.rolling(window).max()
        window_low = shifted_low.rolling(window).min()
        return self._safe_divide(window_high - window_low, window_high) * 100.0

    def _left_base_depth(self) -> pd.DataFrame:
        """% depth of the overall (left-side) VCP base over
        `vcp_base_window` sessions."""
        cfg = self.config
        base_high = self.close.rolling(cfg.vcp_base_window).max()
        base_low = self.close.rolling(cfg.vcp_base_window).min()
        return self._safe_divide(base_high - base_low, base_high) * 100.0

    def _right_pivot_depth(self) -> pd.DataFrame:
        """% depth of the most recent ("right side") pivot leg over
        `vcp_recent_window` sessions."""
        return self._range_pct(self.high, self.low, self.config.vcp_recent_window, shift_by=0)

    # -- Granular scoring metrics (0-100) ------------------------------------ #

    def calculate_volume_dry_up(self) -> pd.DataFrame:
        """0-100: higher when the `volume_dryup_short_window`-session
        average volume is lower relative to the
        `volume_dryup_long_window`-session average (right-side-of-base
        volume contraction).

        Returns:
            Float DataFrame aligned to `self.volume`, clipped to [0, 100].
        """
        cfg = self.config
        short_avg = self.volume.rolling(cfg.volume_dryup_short_window).mean()
        long_avg = self.volume.rolling(cfg.volume_dryup_long_window).mean()
        ratio = self._safe_divide(short_avg, long_avg)
        score = (1.0 - ratio) * 100.0
        return score.clip(lower=0.0, upper=100.0)

    def calculate_contraction_score(self) -> pd.DataFrame:
        """0-100: higher when the base's right-side (pivot) depth is
        tighter relative to its left-side (overall base) depth.

        Returns:
            Float DataFrame aligned to `self.close`, clipped to [0, 100].
        """
        left_depth = self._left_base_depth()
        right_depth = self._right_pivot_depth()
        ratio = self._safe_divide(right_depth, left_depth)
        score = (1.0 - ratio) * 100.0
        return score.clip(lower=0.0, upper=100.0)

    def calculate_pivot_quality(self) -> pd.DataFrame:
        """0-100: today's closing range, ((Close-Low)/(High-Low))*100.
        50.0 on sessions where High == Low.

        Returns:
            Float DataFrame aligned to `self.close`.
        """
        span = self.high - self.low
        safe_span = span.where(span != 0, other=np.nan)
        value = ((self.close - self.low) / safe_span) * 100.0
        value = value.clip(lower=0.0, upper=100.0)
        return value.where(span != 0, other=50.0)

    def calculate_base_quality(self) -> pd.DataFrame:
        """0-100: higher when the Average True Range has shrunk over the
        last `base_quality_lookback` sessions (tighter, lower-volatility
        price action).

        Returns:
            Float DataFrame aligned to `self.close`, clipped to [0, 100].
        """
        cfg = self.config
        prev_close = self.close.shift(1)
        range_hl = self.high - self.low
        range_hc = (self.high - prev_close).abs()
        range_lc = (self.low - prev_close).abs()
        true_range = range_hl.where(range_hl >= range_hc, range_hc)
        true_range = true_range.where(true_range >= range_lc, range_lc)
        atr = true_range.rolling(cfg.atr_window).mean()
        atr_lagged = atr.shift(cfg.base_quality_lookback)

        shrink_pct = self._safe_divide(atr_lagged - atr, atr_lagged) * 100.0
        return shrink_pct.clip(lower=0.0, upper=100.0)

    def calculate_risk_reward_pct(self) -> pd.DataFrame:
        """% risk from today's close down to the trailing
        `stop_loss_lookback_days`-session low.

        Returns:
            Float DataFrame aligned to `self.close`.
        """
        stop_loss = self.low.rolling(self.config.stop_loss_lookback_days).min()
        return self._safe_divide(self.close - stop_loss, self.close) * 100.0

    def calculate_confidence(
        self,
        trend_template_pass: pd.DataFrame,
        base_quality: pd.DataFrame,
        contraction_score: pd.DataFrame,
        volume_dry_up: pd.DataFrame,
        pivot_quality: pd.DataFrame,
    ) -> pd.DataFrame:
        """0-100 weighted composite of the four granular scores, forced to
        0.0 wherever `trend_template_pass` is False.

        Args:
            trend_template_pass: As returned by `calculate_trend_template`.
            base_quality, contraction_score, volume_dry_up, pivot_quality:
                As returned by their respective calculators.

        Returns:
            Float DataFrame aligned to `self.close`.
        """
        cfg = self.config
        weights_sum = cfg.confidence_weights_sum
        if weights_sum <= 0:
            composite = pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
        else:
            composite = (
                base_quality * cfg.weight_base_quality
                + contraction_score * cfg.weight_contraction
                + volume_dry_up * cfg.weight_volume_dryup
                + pivot_quality * cfg.weight_pivot_quality
            ) / weights_sum

        return composite.where(trend_template_pass, other=0.0)

    # -- public API ---------------------------------------------------------- #

    def generate_analysis(self) -> Dict[str, BreakoutAnalysis]:
        """Runs the full vectorized pipeline and packages the latest state
        for every ticker.

        Returns:
            Dict mapping ticker -> BreakoutAnalysis for every ticker
            present in the input.
        """
        if self.close.empty:
            logger.warning("BreakoutEngine.generate_analysis: no data after normalization")
            return {}

        logger.debug(
            "BreakoutEngine.generate_analysis: %d tickers, %d sessions",
            self.close.shape[1], self.close.shape[0],
        )
        trend_template = self.calculate_trend_template()
        high_tight_flag = self.calculate_high_tight_flag()
        flat_base = self.calculate_flat_base()
        pocket_pivot = self.calculate_pocket_pivot()
        vcp = self.calculate_vcp()
        cup_and_handle = self.calculate_cup_and_handle()

        base_quality = self.calculate_base_quality()
        contraction_score = self.calculate_contraction_score()
        volume_dry_up = self.calculate_volume_dry_up()
        pivot_quality = self.calculate_pivot_quality()
        risk_reward_pct = self.calculate_risk_reward_pct()
        confidence = self.calculate_confidence(
            trend_template, base_quality, contraction_score, volume_dry_up, pivot_quality
        )

        latest_date = self.close.index[-1]
        pattern_frames = {
            "High Tight Flag": high_tight_flag,
            "Flat Base": flat_base,
            "Pocket Pivot": pocket_pivot,
            "VCP": vcp,
            "Cup & Handle": cup_and_handle,
        }

        results: Dict[str, BreakoutAnalysis] = {}
        for ticker in self.close.columns:
            passed = bool(trend_template.loc[latest_date, ticker]) if pd.notna(
                trend_template.loc[latest_date, ticker]
            ) else False

            detected = []
            if passed:
                for name, frame in pattern_frames.items():
                    value = frame.loc[latest_date, ticker]
                    if pd.notna(value) and bool(value):
                        detected.append(name)

            results[ticker] = BreakoutAnalysis(
                ticker=ticker,
                trend_template_pass=passed,
                detected_patterns=detected,
                base_quality=self._safe_float(base_quality.loc[latest_date, ticker]),
                contraction_score=self._safe_float(contraction_score.loc[latest_date, ticker]),
                volume_dry_up=self._safe_float(volume_dry_up.loc[latest_date, ticker]),
                pivot_quality=self._safe_float(pivot_quality.loc[latest_date, ticker]),
                risk_reward_pct=self._safe_float(risk_reward_pct.loc[latest_date, ticker]),
                confidence=self._safe_float(confidence.loc[latest_date, ticker]),
            )

        passed_count = sum(1 for r in results.values() if r.trend_template_pass)
        pattern_count = sum(1 for r in results.values() if r.detected_patterns)
        logger.info(
            "BreakoutEngine.generate_analysis: %d/%d ticker(s) passed trend template, "
            "%d with at least one detected pattern",
            passed_count, len(results), pattern_count,
        )
        return results

    @staticmethod
    def _safe_float(value) -> float:
        """Converts a scalar to float, preserving NaN instead of raising."""
        return float(value) if pd.notna(value) else np.nan

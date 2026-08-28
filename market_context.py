"""
Institutional Market Context Engine (IAS Plug-in Module)

Computes a composite "market context" reading — a regime score, confidence,
velocity and its four underlying components (trend, volatility, breadth,
liquidity) — from raw market inputs (NIFTY, BANKNIFTY, India VIX, advance/
decline data, breadth data and sector breadth).

Design notes
------------
- This module is fully standalone: it does not import DataBroker, config,
  scanner_engine or any other existing Version 1.0 module, and nothing in
  the existing codebase imports it. It is safe to add without any risk of
  regressing the production scanner.
- All public types are frozen dataclasses (immutable, hashable, easy to
  assert against in tests).
- No global/module-level mutable state. All state lives on instances of
  MarketContextEngine or is passed explicitly via inputs.
- Every calculation method takes explicit arguments and returns a value or
  a dataclass, making each one independently unit testable without needing
  to construct the full engine or any live data feed.
- Logs at DEBUG for component-level calculations and INFO for the final
  composite reading, via the standard `logging` module (module-level
  `logger`, no global mutable state) — configure handlers/levels in the
  caller's application, this module never calls `logging.basicConfig()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Input dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AdvanceDeclineData:
    """Raw market-wide advance/decline counts for a single session.

    Attributes:
        advances: Number of advancing stocks.
        declines: Number of declining stocks.
        unchanged: Number of unchanged stocks.
    """
    advances: int
    declines: int
    unchanged: int = 0

    @property
    def ratio(self) -> float:
        """Advance/Decline ratio. Returns a large finite number instead of
        raising or returning inf when there are zero declines."""
        if self.declines <= 0:
            return float(self.advances) if self.advances > 0 else 1.0
        return self.advances / self.declines

    @property
    def total(self) -> int:
        return self.advances + self.declines + self.unchanged


@dataclass(frozen=True)
class BreadthData:
    """Market breadth snapshot.

    Attributes:
        pct_above_50dma: % of universe stocks trading above their 50-day MA.
        pct_above_200dma: % of universe stocks trading above their 200-day MA.
            Optional — pass None if not available.
    """
    pct_above_50dma: float
    pct_above_200dma: Optional[float] = None


@dataclass(frozen=True)
class MarketContextInputs:
    """Container for all raw inputs required by MarketContextEngine.analyze().

    Attributes:
        nifty: OHLCV DataFrame for NIFTY 50, most-recent row last. Must
            contain a 'Close' column; a 'Volume' column is optional and used
            for the liquidity component when present.
        banknifty: OHLCV DataFrame for BANKNIFTY, same shape rules as nifty.
        india_vix: OHLCV DataFrame for India VIX. Must contain a 'Close'
            column.
        advance_decline: AdvanceDeclineData for the session.
        breadth: BreadthData for the session.
        sector_breadth: Mapping of sector name -> % of that sector's stocks
            trading above their key moving average (0-100). Used to gauge
            how broad-based (vs. narrow/concentrated) the move is.
    """
    nifty: pd.DataFrame
    banknifty: pd.DataFrame
    india_vix: pd.DataFrame
    advance_decline: AdvanceDeclineData
    breadth: BreadthData
    sector_breadth: Dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Component & output dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TrendComponent:
    """Directional health of the two headline indices.

    Attributes:
        score: 0-100 sub-score.
        nifty_state: 'BULL' | 'BEAR' | 'UNKNOWN'.
        banknifty_state: 'BULL' | 'BEAR' | 'UNKNOWN'.
        details: Supporting figures (EMA values, % distance from EMA, etc).
    """
    score: float
    nifty_state: str
    banknifty_state: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VolatilityComponent:
    """India VIX based fear/complacency reading.

    Attributes:
        score: 0-100 sub-score (higher = calmer / more favorable).
        vix_level: Latest India VIX close.
        vix_state: 'LOW' | 'NEUTRAL' | 'HIGH' | 'UNKNOWN'.
        vix_change_pct: Session-over-session % change in VIX, if derivable.
    """
    score: float
    vix_level: float
    vix_state: str
    vix_change_pct: Optional[float] = None


@dataclass(frozen=True)
class BreadthComponent:
    """Participation / internals of the move.

    Attributes:
        score: 0-100 sub-score.
        advance_decline_ratio: Advances / declines for the session.
        pct_above_50dma: % of universe above 50-DMA.
        sector_participation_pct: % of tracked sectors themselves showing
            breadth > 50 (i.e. how many sectors are internally healthy).
    """
    score: float
    advance_decline_ratio: float
    pct_above_50dma: float
    sector_participation_pct: float


@dataclass(frozen=True)
class LiquidityComponent:
    """Participation measured via traded volume vs its recent average.

    Attributes:
        score: 0-100 sub-score.
        nifty_volume_ratio: Latest NIFTY volume / its 20-period average.
            None when volume data isn't available (component then defaults
            to a neutral score).
        banknifty_volume_ratio: Same, for BANKNIFTY.
    """
    score: float
    nifty_volume_ratio: Optional[float] = None
    banknifty_volume_ratio: Optional[float] = None


@dataclass(frozen=True)
class MarketContext:
    """Final composite output of MarketContextEngine.analyze().

    Attributes:
        regime: Human-readable label, e.g. 'STRONG BULL', 'NEUTRAL', 'BEAR'.
        score: Composite Market Regime Score, 0-100.
        confidence: Confidence in `score`, 0-100 — driven by input
            completeness and agreement between the four components.
        velocity: Change in `score` versus the previous reading (points per
            call). 0.0 when no previous score was supplied.
        trend: TrendComponent.
        volatility: VolatilityComponent.
        breadth: BreadthComponent.
        liquidity: LiquidityComponent.
    """
    regime: str
    score: float
    confidence: float
    velocity: float
    trend: TrendComponent
    volatility: VolatilityComponent
    breadth: BreadthComponent
    liquidity: LiquidityComponent


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MarketContextConfig:
    """Tunable weights and thresholds for MarketContextEngine.

    Component weights must sum to 100 for `score` to land on a clean 0-100
    scale; this is not enforced so experimentation stays cheap, but
    `weights_sum` is exposed to make deviations easy to spot in tests.

    Attributes:
        weight_trend: Weight of TrendComponent in the composite score.
        weight_volatility: Weight of VolatilityComponent.
        weight_breadth: Weight of BreadthComponent.
        weight_liquidity: Weight of LiquidityComponent.
        vix_low_threshold: Below this, VIX is considered 'LOW'.
        vix_high_threshold: At/above this, VIX is considered 'HIGH'.
        regime_thresholds: Ordered (min_score, label) pairs, highest first,
            used to map the composite score to a regime label.
    """
    weight_trend: float = 35.0
    weight_volatility: float = 20.0
    weight_breadth: float = 30.0
    weight_liquidity: float = 15.0

    vix_low_threshold: float = 15.0
    vix_high_threshold: float = 25.0

    regime_thresholds: tuple = (
        (75.0, "STRONG BULL"),
        (60.0, "BULL"),
        (40.0, "NEUTRAL"),
        (20.0, "WEAK"),
        (0.0, "BEAR"),
    )

    @property
    def weights_sum(self) -> float:
        return (
            self.weight_trend
            + self.weight_volatility
            + self.weight_breadth
            + self.weight_liquidity
        )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class MarketContextEngine:
    """Stateless-by-default calculator that turns MarketContextInputs into a
    MarketContext reading.

    The engine holds no global state. Instance state is limited to its
    `config` and an optional `previous_score`, both supplied by the caller,
    so multiple engines (e.g. per-strategy, per-timeframe) can run side by
    side without interfering with one another.

    Example:
        engine = MarketContextEngine(MarketContextConfig())
        context = engine.analyze(inputs)
    """

    def __init__(
        self,
        config: Optional[MarketContextConfig] = None,
        previous_score: Optional[float] = None,
    ):
        """
        Args:
            config: Weights/thresholds to use. Defaults to
                MarketContextConfig() if not supplied.
            previous_score: The composite `score` from the prior reading,
                used to compute `velocity`. Pass None for a cold start
                (velocity will be reported as 0.0).
        """
        self.config = config or MarketContextConfig()
        self.previous_score = previous_score

    # -- component calculators ------------------------------------------- #

    def _index_state(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Derives BULL/BEAR state and EMA distances for one index."""
        if df is None or df.empty or "Close" not in df.columns or len(df) < 50:
            return {"state": "UNKNOWN", "close": None, "ema20": None, "ema50": None}

        close = df["Close"]
        ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        latest = close.iloc[-1]
        state = "BULL" if latest > ema50 else "BEAR"
        return {"state": state, "close": float(latest), "ema20": float(ema20), "ema50": float(ema50)}

    def calculate_trend(self, inputs: MarketContextInputs) -> TrendComponent:
        """Scores directional health using NIFTY (primary) and BANKNIFTY
        (confirmation) close vs their EMA50/EMA20.

        Scoring (0-100):
            NIFTY BULL + above EMA20 (strong): 60
            NIFTY BULL only:                   40
            BANKNIFTY confirms BULL:           +40
            (BEAR / UNKNOWN indices contribute 0 for that leg)
        """
        nifty_info = self._index_state(inputs.nifty)
        bn_info = self._index_state(inputs.banknifty)

        score = 0.0
        if nifty_info["state"] == "BULL":
            score += 40.0
            if nifty_info["close"] is not None and nifty_info["ema20"] is not None:
                if nifty_info["close"] > nifty_info["ema20"]:
                    score += 20.0
        if bn_info["state"] == "BULL":
            score += 40.0

        return TrendComponent(
            score=min(score, 100.0),
            nifty_state=nifty_info["state"],
            banknifty_state=bn_info["state"],
            details={"nifty": nifty_info, "banknifty": bn_info},
        )

    def calculate_volatility(self, inputs: MarketContextInputs) -> VolatilityComponent:
        """Scores India VIX level: lower VIX -> higher (more favorable) score.

        Scoring (0-100):
            LOW (< vix_low_threshold):                         100
            NEUTRAL (vix_low_threshold..vix_high_threshold):   linearly 30-70
            HIGH (>= vix_high_threshold):                       0
        """
        df = inputs.india_vix
        if df is None or df.empty or "Close" not in df.columns:
            return VolatilityComponent(score=50.0, vix_level=float("nan"), vix_state="UNKNOWN")

        latest_vix = float(df["Close"].iloc[-1])
        change_pct = None
        if len(df) >= 2:
            prev = float(df["Close"].iloc[-2])
            if prev != 0:
                change_pct = ((latest_vix - prev) / prev) * 100.0

        low, high = self.config.vix_low_threshold, self.config.vix_high_threshold
        if latest_vix < low:
            score = 100.0
            state = "LOW"
        elif latest_vix >= high:
            score = 0.0
            state = "HIGH"
        else:
            # Linear interpolation between low->70 and high->30, inverted (lower vix = higher score)
            span = high - low
            position = (latest_vix - low) / span if span > 0 else 0.5
            score = 70.0 - (position * 40.0)
            state = "NEUTRAL"

        return VolatilityComponent(
            score=max(0.0, min(score, 100.0)),
            vix_level=latest_vix,
            vix_state=state,
            vix_change_pct=change_pct,
        )

    def calculate_breadth(self, inputs: MarketContextInputs) -> BreadthComponent:
        """Scores participation via advance/decline ratio, % above 50-DMA
        and how many sectors are themselves breadth-healthy.

        Scoring (0-100), weighted 40% AD ratio / 40% pct-above-50dma / 20%
        sector participation.
        """
        ad_ratio = inputs.advance_decline.ratio
        # Map AD ratio to 0-100: 1.0 -> 50, 3.0+ -> 100, 0.33- -> 0 (roughly symmetric in log space)
        if ad_ratio <= 0:
            ad_score = 0.0
        else:
            import math
            ad_score = 50.0 + (math.log(ad_ratio) / math.log(3.0)) * 50.0
            ad_score = max(0.0, min(ad_score, 100.0))

        pct_above = max(0.0, min(inputs.breadth.pct_above_50dma, 100.0))

        sector_values = list(inputs.sector_breadth.values())
        if sector_values:
            sector_participation = (
                sum(1 for v in sector_values if v > 50.0) / len(sector_values)
            ) * 100.0
        else:
            sector_participation = 50.0  # neutral default when unknown

        score = (ad_score * 0.4) + (pct_above * 0.4) + (sector_participation * 0.2)

        return BreadthComponent(
            score=max(0.0, min(score, 100.0)),
            advance_decline_ratio=ad_ratio,
            pct_above_50dma=pct_above,
            sector_participation_pct=sector_participation,
        )

    def _volume_ratio(self, df: pd.DataFrame, lookback: int = 20) -> Optional[float]:
        """Latest volume / trailing average volume, or None if unavailable."""
        if df is None or df.empty or "Volume" not in df.columns or len(df) < lookback + 1:
            return None
        recent_avg = df["Volume"].iloc[-(lookback + 1):-1].mean()
        if recent_avg == 0 or pd.isna(recent_avg):
            return None
        return float(df["Volume"].iloc[-1] / recent_avg)

    def calculate_liquidity(self, inputs: MarketContextInputs) -> LiquidityComponent:
        """Scores participation via traded volume vs its 20-period average
        for NIFTY and BANKNIFTY.

        Scoring (0-100): ratio of 1.0 (average volume) -> 50, 2.0+ -> 100,
        0.0 -> 0. Defaults to a neutral 50 when volume data isn't available
        for either index.
        """
        nifty_ratio = self._volume_ratio(inputs.nifty)
        bn_ratio = self._volume_ratio(inputs.banknifty)

        ratios = [r for r in (nifty_ratio, bn_ratio) if r is not None]
        if not ratios:
            score = 50.0
        else:
            avg_ratio = sum(ratios) / len(ratios)
            score = max(0.0, min(avg_ratio * 50.0, 100.0))

        return LiquidityComponent(
            score=score,
            nifty_volume_ratio=nifty_ratio,
            banknifty_volume_ratio=bn_ratio,
        )

    # -- composite calculators --------------------------------------------- #

    def _composite_score(
        self,
        trend: TrendComponent,
        volatility: VolatilityComponent,
        breadth: BreadthComponent,
        liquidity: LiquidityComponent,
    ) -> float:
        """Weighted sum of the four component scores, normalized by the
        configured weight total so an off-100 weights_sum still yields a
        valid 0-100 composite."""
        weights_sum = self.config.weights_sum
        if weights_sum <= 0:
            return 0.0
        raw = (
            trend.score * self.config.weight_trend
            + volatility.score * self.config.weight_volatility
            + breadth.score * self.config.weight_breadth
            + liquidity.score * self.config.weight_liquidity
        )
        return max(0.0, min(raw / weights_sum, 100.0))

    def _confidence(
        self,
        inputs: MarketContextInputs,
        trend: TrendComponent,
        volatility: VolatilityComponent,
        breadth: BreadthComponent,
        liquidity: LiquidityComponent,
    ) -> float:
        """Confidence % combines two factors:

        1. Completeness: fraction of inputs that were actually resolvable
           (indices not UNKNOWN, VIX known, sector breadth provided, volume
           data present) — an engine forced to fall back to neutral
           defaults is less trustworthy.
        2. Agreement: how tightly the four component scores cluster. Four
           components pointing the same direction is a stronger signal than
           four components disagreeing, even if the average is identical.
        """
        completeness_checks = [
            trend.nifty_state != "UNKNOWN",
            trend.banknifty_state != "UNKNOWN",
            volatility.vix_state != "UNKNOWN",
            bool(inputs.sector_breadth),
            liquidity.nifty_volume_ratio is not None,
        ]
        completeness = sum(1 for c in completeness_checks if c) / len(completeness_checks)

        scores = [trend.score, volatility.score, breadth.score, liquidity.score]
        mean_score = sum(scores) / len(scores)
        variance = sum((s - mean_score) ** 2 for s in scores) / len(scores)
        std_dev = variance ** 0.5
        # std_dev of 0 -> perfect agreement (1.0); std_dev of 50+ -> no agreement (0.0)
        agreement = max(0.0, 1.0 - (std_dev / 50.0))

        confidence = ((completeness * 0.5) + (agreement * 0.5)) * 100.0
        return max(0.0, min(confidence, 100.0))

    def _regime_label(self, score: float) -> str:
        """Maps a composite score to a regime label using
        `config.regime_thresholds` (checked highest-first)."""
        for threshold, label in self.config.regime_thresholds:
            if score >= threshold:
                return label
        return self.config.regime_thresholds[-1][1]

    # -- public API ---------------------------------------------------------- #

    def analyze(self, inputs: MarketContextInputs) -> MarketContext:
        """Runs all component calculators and returns the composite
        MarketContext reading.

        Args:
            inputs: A fully populated MarketContextInputs instance.

        Returns:
            MarketContext with the composite regime/score/confidence/
            velocity plus each underlying component.
        """
        logger.debug("MarketContextEngine.analyze: starting component calculations")
        trend = self.calculate_trend(inputs)
        volatility = self.calculate_volatility(inputs)
        breadth = self.calculate_breadth(inputs)
        liquidity = self.calculate_liquidity(inputs)
        logger.debug(
            "MarketContextEngine.analyze: component scores trend=%.2f volatility=%.2f "
            "breadth=%.2f liquidity=%.2f",
            trend.score, volatility.score, breadth.score, liquidity.score,
        )

        score = self._composite_score(trend, volatility, breadth, liquidity)
        confidence = self._confidence(inputs, trend, volatility, breadth, liquidity)
        velocity = 0.0 if self.previous_score is None else score - self.previous_score
        regime = self._regime_label(score)
        logger.info(
            "MarketContextEngine.analyze: regime=%s score=%.2f confidence=%.2f velocity=%+.2f",
            regime, score, confidence, velocity,
        )

        return MarketContext(
            regime=regime,
            score=score,
            confidence=confidence,
            velocity=velocity,
            trend=trend,
            volatility=volatility,
            breadth=breadth,
            liquidity=liquidity,
        )

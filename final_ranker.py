"""
Final Ranker / Adaptive Position-Sizing Orchestrator (IAS Plug-in Module)

Master orchestration stage that combines the per-ticker outputs of the
earlier IAS stages (market context, sector rotation, relative strength,
institutional flow, breakout pattern recognition) into a single
regime-aware Alpha Score, probability estimate, system confidence and
position size.

Design notes
------------
- Completely independent: no imports from config.py, data_broker.py,
  scanner_engine.py, or any of the other IAS modules (market_context.py,
  sector_rotation.py, relative_strength.py, institutional_flow.py,
  breakout_engine.py). This module only depends on the *shape* of their
  outputs (ticker, regime, percentile, score, etc.), expressed here as its
  own small, stable input dataclasses — so it can't be broken by internal
  refactors of the upstream engines, and adding it carries zero regression
  risk to Version 1.0 or to the other plug-in modules.
- Pure orchestrator: does not compute any raw technicals, moving averages,
  or relative strength — it only combines already-computed scalar
  dataclass fields.
- No hardcoded float weights: every weight, multiplier, and base position
  size is read from `ranker_config.yaml` via PyYAML. If the file is
  missing, unreadable, or malformed, `RankerConfig.load()` logs a warning
  and falls back to an in-code default that mirrors the shipped YAML, so
  the ranker degrades gracefully instead of crashing.
- No global/module-level mutable state. All state lives on instances of
  RankerConfig / AdaptiveRanker, passed or loaded explicitly.
- Logs at DEBUG for per-ticker gate/scoring decisions, WARNING for config
  fallback (mirrored to `warnings.warn` for callers who only watch Python
  warnings), and INFO for the universe-level evaluation summary, via a
  module-level `logger` (standard `logging` module).
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

try:
    import yaml

    _YAML_AVAILABLE = True
except ImportError:  # pragma: no cover - environment without PyYAML installed
    yaml = None
    _YAML_AVAILABLE = False


DEFAULT_CONFIG_PATH = "ranker_config.yaml"


# --------------------------------------------------------------------------- #
# Input dataclasses (this module's stable contract with Stage 1-5 outputs)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MarketContextInput:
    """Subset of market_context.MarketContext this stage needs.

    Attributes:
        regime: Regime label, e.g. 'TRENDING_BULL', 'SIDEWAYS', 'BEAR'.
            Must match a key under `regimes:` in ranker_config.yaml (falls
            back to `RankerConfig.default_regime` if it doesn't).
        regime_score: 0-100 composite market regime score.
        confidence: 0-100 confidence in the regime reading.
    """
    regime: str
    regime_score: float
    confidence: float


@dataclass(frozen=True)
class SectorInput:
    """Subset of sector-rotation output this stage needs.

    Attributes:
        sector_multiplier: Multiplier applied to the base score to reward
            (>1.0) or penalize (<1.0) stocks in currently favored/out-of-
            favor sectors.
    """
    sector_multiplier: float


@dataclass(frozen=True)
class RSInput:
    """Subset of relative_strength output this stage needs.

    Attributes:
        rs_percentile: 0-100 (or similar) relative strength percentile.
        rs_acceleration: Change in RS percentile/score versus a prior
            reading. Not used in the current scoring formula but carried
            through for callers/future weighting.
    """
    rs_percentile: float
    rs_acceleration: float


@dataclass(frozen=True)
class InstitutionalInput:
    """Subset of institutional_flow output this stage needs.

    Attributes:
        institutional_score: 0-100 composite institutional footprint score.
    """
    institutional_score: float


@dataclass(frozen=True)
class BreakoutInput:
    """Subset of breakout_engine output this stage needs.

    Attributes:
        confidence: 0-100 breakout-pattern confidence.
        risk_reward_pct: % distance from entry to logical stop-loss.
        trend_template_pass: Hard gate — if False, this stage returns a
            zeroed-out result without evaluating anything else.
    """
    confidence: float
    risk_reward_pct: float
    trend_template_pass: bool


@dataclass(frozen=True)
class TickerInputs:
    """Combined per-ticker input bundle for AdaptiveRanker.

    Attributes:
        ticker: Stock ticker.
        market_context, sector_data, rs_data, inst_data, breakout_data:
            The Stage 1-5 output subsets described above.
    """
    ticker: str
    market_context: MarketContextInput
    sector_data: SectorInput
    rs_data: RSInput
    inst_data: InstitutionalInput
    breakout_data: BreakoutInput


# --------------------------------------------------------------------------- #
# Output dataclass
# --------------------------------------------------------------------------- #

@dataclass
class FinalRankResult:
    """Final regime-adjusted ranking output for a single ticker.

    Attributes:
        ticker: Stock ticker.
        alpha_score: 0-100 composite opportunity score (regime-weighted
            RS/institutional/breakout blend, times the sector multiplier,
            capped at 100).
        probability: 0.0-1.0 statistical likelihood of breakout success,
            blended from `alpha_score` and `market_context.confidence`
            per `blend_weights.probability_*` in the config.
        system_confidence: 0-100 blend of breakout-pattern confidence and
            market-context confidence, per
            `blend_weights.system_confidence_*` in the config — a measure
            of how much to trust this read, distinct from `alpha_score`
            (the opportunity's magnitude).
        risk_pct: % distance to the logical stop-loss (passed through from
            `breakout_data.risk_reward_pct`).
        position_size: 0.0-1.0 multiplier of a standard account risk unit
            to deploy. 0.0 whenever the hard trend-template gate fails, or
            when `risk_pct` exceeds the regime's `max_stop_loss_pct`.
    """
    ticker: str
    alpha_score: float
    probability: float
    system_confidence: float
    risk_pct: float
    position_size: float


# --------------------------------------------------------------------------- #
# Configuration (loaded from YAML, with a graceful in-code fallback)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RegimeWeights:
    """Per-regime weights and sizing parameters, as parsed from
    ranker_config.yaml's `regimes.<REGIME>` block.

    Attributes:
        weight_rs: Weight of `rs_data.rs_percentile` in the base score.
        weight_inst_flow: Weight of `inst_data.institutional_score`.
        weight_breakout: Weight of `breakout_data.confidence`.
        base_position_scale: Starting position-size multiplier for this
            regime before risk-based scaling.
        max_stop_loss_pct: Stop-loss % above which position size is
            forced to 0.0 for this regime.
    """
    weight_rs: float
    weight_inst_flow: float
    weight_breakout: float
    base_position_scale: float
    max_stop_loss_pct: float

    @property
    def weights_sum(self) -> float:
        return self.weight_rs + self.weight_inst_flow + self.weight_breakout


@dataclass(frozen=True)
class RankerConfig:
    """Parsed, ready-to-use configuration for AdaptiveRanker.

    Attributes:
        standard_stop_pct: Reference stop-loss % used as the baseline for
            inverse risk-based position-size scaling.
        probability_alpha_weight: Weight of `alpha_score` in the
            `probability` blend.
        probability_market_confidence_weight: Weight of
            `market_context.confidence` in the `probability` blend.
        system_confidence_breakout_weight: Weight of
            `breakout_data.confidence` in the `system_confidence` blend.
        system_confidence_market_weight: Weight of
            `market_context.confidence` in the `system_confidence` blend.
        regimes: Mapping of regime label -> RegimeWeights.
        default_regime: Regime used when a ticker's
            `market_context.regime` isn't a recognized key in `regimes`.
    """
    standard_stop_pct: float
    probability_alpha_weight: float
    probability_market_confidence_weight: float
    system_confidence_breakout_weight: float
    system_confidence_market_weight: float
    regimes: Dict[str, RegimeWeights]
    default_regime: str = "SIDEWAYS"

    def get_regime_weights(self, regime: Optional[str]) -> RegimeWeights:
        """Looks up `regime`'s weights, falling back to `default_regime`
        (and, if that's also missing, to the first configured regime) so a
        ticker with an unrecognized regime label is always scoreable.

        Args:
            regime: Regime label from `market_context.regime`.

        Returns:
            RegimeWeights for `regime`, or the fallback regime's weights.
        """
        if regime in self.regimes:
            return self.regimes[regime]
        if self.default_regime in self.regimes:
            return self.regimes[self.default_regime]
        return next(iter(self.regimes.values()))

    # -- construction --------------------------------------------------- #

    @staticmethod
    def _default_config_dict() -> dict:
        """Built-in fallback config, mirroring the shipped
        ranker_config.yaml. Returns a fresh dict each call so callers
        can't accidentally mutate shared state."""
        return {
            "standard_stop_pct": 5.0,
            "blend_weights": {
                "probability_alpha_weight": 0.7,
                "probability_market_confidence_weight": 0.3,
                "system_confidence_breakout_weight": 0.5,
                "system_confidence_market_weight": 0.5,
            },
            "regimes": {
                "TRENDING_BULL": {
                    "weight_rs": 0.40,
                    "weight_inst_flow": 0.30,
                    "weight_breakout": 0.30,
                    "base_position_scale": 1.0,
                    "max_stop_loss_pct": 8.0,
                },
                "SIDEWAYS": {
                    "weight_rs": 0.30,
                    "weight_inst_flow": 0.30,
                    "weight_breakout": 0.40,
                    "base_position_scale": 0.5,
                    "max_stop_loss_pct": 6.0,
                },
                "BEAR": {
                    "weight_rs": 0.20,
                    "weight_inst_flow": 0.20,
                    "weight_breakout": 0.60,
                    "base_position_scale": 0.25,
                    "max_stop_loss_pct": 4.0,
                },
            },
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "RankerConfig":
        """Parses a raw (already-loaded) config dict into a RankerConfig.

        Args:
            raw: Dict matching the ranker_config.yaml structure.

        Raises:
            KeyError, TypeError: If `raw` is missing required keys or has
                the wrong shape. Callers (`load`) are expected to catch
                these and fall back to `_default_config_dict()`.
        """
        blend = raw["blend_weights"]
        regimes = {
            name: RegimeWeights(
                weight_rs=float(block["weight_rs"]),
                weight_inst_flow=float(block["weight_inst_flow"]),
                weight_breakout=float(block["weight_breakout"]),
                base_position_scale=float(block["base_position_scale"]),
                max_stop_loss_pct=float(block["max_stop_loss_pct"]),
            )
            for name, block in raw["regimes"].items()
        }
        if not regimes:
            raise ValueError("ranker_config.yaml has no entries under 'regimes'")

        return cls(
            standard_stop_pct=float(raw["standard_stop_pct"]),
            probability_alpha_weight=float(blend["probability_alpha_weight"]),
            probability_market_confidence_weight=float(blend["probability_market_confidence_weight"]),
            system_confidence_breakout_weight=float(blend["system_confidence_breakout_weight"]),
            system_confidence_market_weight=float(blend["system_confidence_market_weight"]),
            regimes=regimes,
        )

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "RankerConfig":
        """Loads and parses `path` via PyYAML. On any failure — file
        missing, unreadable, invalid YAML, or missing/malformed expected
        keys — logs a warning and falls back to the built-in default
        config instead of raising, so AdaptiveRanker always has usable
        weights.

        Args:
            path: Path to a YAML file matching the ranker_config.yaml
                structure.

        Returns:
            RankerConfig, either parsed from `path` or the built-in
            default.
        """
        if not _YAML_AVAILABLE:
            message = (
                "final_ranker: PyYAML is not installed; falling back to the "
                "built-in default ranker configuration."
            )
            logger.warning(message)
            warnings.warn(message, RuntimeWarning)
            return cls.from_dict(cls._default_config_dict())

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            if not isinstance(raw, dict):
                raise ValueError(f"{path} did not parse to a mapping")
            config = cls.from_dict(raw)
            logger.info("RankerConfig.load: loaded config from '%s' (%d regimes)", path, len(config.regimes))
            return config
        except (OSError, ValueError, KeyError, TypeError) as exc:
            message = (
                f"final_ranker: could not load '{path}' ({exc!r}); falling back "
                "to the built-in default ranker configuration."
            )
            logger.warning(message)
            warnings.warn(message, RuntimeWarning)
            return cls.from_dict(cls._default_config_dict())
        except yaml.YAMLError as exc:  # malformed YAML syntax
            message = (
                f"final_ranker: '{path}' is not valid YAML ({exc!r}); falling "
                "back to the built-in default ranker configuration."
            )
            logger.warning(message)
            warnings.warn(message, RuntimeWarning)
            return cls.from_dict(cls._default_config_dict())


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

class AdaptiveRanker:
    """Combines Stage 1-5 outputs into a final, regime-aware ranking per
    ticker. Performs no raw technical calculation of its own.

    Example:
        config = RankerConfig.load("ranker_config.yaml")
        ranker = AdaptiveRanker(universe_data, config)
        results = ranker.evaluate_universe()
    """

    def __init__(
        self,
        universe_data: Dict[str, TickerInputs],
        config: Optional[RankerConfig] = None,
    ):
        """
        Args:
            universe_data: Mapping of ticker -> TickerInputs, one entry
                per stock to be ranked.
            config: Parsed ranker configuration. Defaults to
                `RankerConfig.load(DEFAULT_CONFIG_PATH)` if not supplied.
        """
        self.universe_data = universe_data
        self.config = config or RankerConfig.load()

    def evaluate_ticker(self, inputs: TickerInputs) -> FinalRankResult:
        """Scores a single ticker.

        Args:
            inputs: TickerInputs for one stock.

        Returns:
            FinalRankResult for that stock.
        """
        breakout = inputs.breakout_data

        # -- 1. Hard filter -------------------------------------------- #
        if not breakout.trend_template_pass:
            logger.debug("evaluate_ticker: %s gated out (trend_template_pass=False)", inputs.ticker)
            return FinalRankResult(
                ticker=inputs.ticker,
                alpha_score=0.0,
                probability=0.0,
                system_confidence=0.0,
                risk_pct=breakout.risk_reward_pct,
                position_size=0.0,
            )

        # -- 2. Regime-adjusted weighting -------------------------------- #
        weights = self.config.get_regime_weights(inputs.market_context.regime)

        base_score = (
            inputs.rs_data.rs_percentile * weights.weight_rs
            + inputs.inst_data.institutional_score * weights.weight_inst_flow
            + breakout.confidence * weights.weight_breakout
        )

        # -- 3. Sector alpha multiplier ------------------------------------ #
        alpha_score = base_score * inputs.sector_data.sector_multiplier
        alpha_score = max(0.0, min(alpha_score, 100.0))

        # -- 4. Position sizing & risk engine ------------------------------- #
        risk_pct = breakout.risk_reward_pct
        if risk_pct is None or risk_pct != risk_pct:  # NaN-safe check
            position_size = 0.0
        elif risk_pct <= 0 or risk_pct > weights.max_stop_loss_pct:
            position_size = 0.0
        else:
            risk_multiplier = min(1.0, self.config.standard_stop_pct / risk_pct)
            position_size = weights.base_position_scale * risk_multiplier
            position_size = max(0.0, min(position_size, 1.0))

        # -- derived composites -------------------------------------------- #
        cfg = self.config
        probability = (
            alpha_score * cfg.probability_alpha_weight
            + inputs.market_context.confidence * cfg.probability_market_confidence_weight
        ) / 100.0
        probability = max(0.0, min(probability, 1.0))

        system_confidence = (
            breakout.confidence * cfg.system_confidence_breakout_weight
            + inputs.market_context.confidence * cfg.system_confidence_market_weight
        )
        system_confidence = max(0.0, min(system_confidence, 100.0))

        logger.debug(
            "evaluate_ticker: %s regime=%s alpha=%.2f position_size=%.2f risk_pct=%s",
            inputs.ticker, inputs.market_context.regime, alpha_score, position_size, risk_pct,
        )
        return FinalRankResult(
            ticker=inputs.ticker,
            alpha_score=alpha_score,
            probability=probability,
            system_confidence=system_confidence,
            risk_pct=risk_pct,
            position_size=position_size,
        )

    def evaluate_universe(self) -> Dict[str, FinalRankResult]:
        """Scores every ticker in `self.universe_data`.

        This stage combines already-computed scalar fields rather than
        performing bulk array math, so it iterates per ticker by design
        (unlike the vectorized Stage 1-5 engines) — there's no DataFrame
        of raw prices here to vectorize over.

        Returns:
            Dict mapping ticker -> FinalRankResult.
        """
        results = {
            ticker: self.evaluate_ticker(inputs)
            for ticker, inputs in self.universe_data.items()
        }
        actionable = sum(1 for r in results.values() if r.position_size > 0)
        logger.info(
            "AdaptiveRanker.evaluate_universe: scored %d ticker(s), %d actionable (position_size > 0)",
            len(results), actionable,
        )
        return results

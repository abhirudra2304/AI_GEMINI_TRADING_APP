"""
Integration Adapter (IAS Plug-in Module)

Pure translation layer between the five scoring engines' real output
dataclasses (market_context.MarketContext, sector_rotation.SectorScore,
relative_strength.RelativeStrengthResult, institutional_flow.InstitutionalFlowResult,
breakout_engine.BreakoutAnalysis) and the TickerInputs schema final_ranker.py
actually consumes.

Why this module exists
-----------------------
Each of the five engines was deliberately built without depending on any of
the others, so none of them agree on a shared vocabulary: market_context's
regime labels ('STRONG BULL' / 'BULL' / 'NEUTRAL' / 'WEAK' / 'BEAR') don't
match ranker_config.yaml's regime keys ('TRENDING_BULL' / 'SIDEWAYS' /
'BEAR'), and sector_rotation.SectorScore has no `sector_multiplier` field at
all — it has `score` and `percentile`. Wiring the five engines directly into
AdaptiveRanker without resolving those mismatches would either crash or
(worse) silently fall back to default regime weights every single day. This
module is where that translation happens, once, in one place.

Design notes
------------
- No modifications to market_context.py, sector_rotation.py,
  relative_strength.py, institutional_flow.py, breakout_engine.py,
  final_ranker.py, or ranker_config.yaml. The regime-taxonomy fix and the
  sector_multiplier derivation both live here, entirely additively.
- Pure translation: no scoring, ranking, or trading logic of its own. Every
  number it produces is either passed through unchanged or run through one
  small, documented, config-driven transform (regime mapping, sector
  multiplier scaling). If a computation looks like "scoring," it belongs in
  one of the five engines, not here.
- All transform parameters live in `AdapterConfig` — no hardcoded magic
  numbers.
- No global/module-level mutable state (the regime map is a frozen,
  read-only constant, not runtime state).
- Every method takes explicit arguments and returns a plain value or
  dataclass, so each is independently unit testable with hand-built engine
  outputs — no live data or running pipeline required.
- Logs at DEBUG for individual field translations and WARNING whenever a
  fallback default is used because an engine's result was missing or
  produced NaN, via a module-level `logger` (standard `logging` module).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from breakout_engine import BreakoutAnalysis
from final_ranker import (
    BreakoutInput,
    InstitutionalInput,
    MarketContextInput,
    RSInput,
    SectorInput,
    TickerInputs,
)
from institutional_flow import InstitutionalFlowResult
from market_context import MarketContext
from relative_strength import RelativeStrengthResult
from sector_rotation import SectorScore

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Regime taxonomy fix
# --------------------------------------------------------------------------- #

# market_context.MarketContextConfig.regime_thresholds labels -> the
# TRENDING_BULL / SIDEWAYS / BEAR taxonomy ranker_config.yaml's `regimes:`
# block actually defines. STRONG BULL and BULL both count as a trending bull
# regime for position-sizing purposes; NEUTRAL and WEAK both get the more
# conservative Sideways weights. This is the fix for the regime-taxonomy
# mismatch identified in the pre-freeze architecture audit: it resolves the
# vocabulary gap without editing either module that disagreed about it.
REGIME_TAXONOMY_MAP: Dict[str, str] = {
    "STRONG BULL": "TRENDING_BULL",
    "BULL": "TRENDING_BULL",
    "NEUTRAL": "SIDEWAYS",
    "WEAK": "SIDEWAYS",
    "BEAR": "BEAR",
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AdapterConfig:
    """Tunable transform parameters for IntegrationAdapter.

    Attributes:
        default_regime: Regime key handed to final_ranker when a
            market_context regime label isn't in `REGIME_TAXONOMY_MAP`
            (e.g. a future label added to market_context.py without a
            corresponding update here).
        min_sector_multiplier: Sector multiplier at sector percentile 0.
        max_sector_multiplier: Sector multiplier at sector percentile 100.
            Percentile 50 always maps to exactly 1.0 (neutral), linearly
            interpolated between the two bounds either side of it.
            Widened from 0.8/1.2 to 0.6/1.4 (2026-07-29): the original
            +-20% band let a strong technical breakout in a weak/lagging
            sector still surface near the top of alpha_picks, discounted
            by at most 20%. In a market with aggressive sector rotation
            (money moving cleanly between sectors rather than diffusing
            broadly), that's too weak a penalty - +-40% lets sector
            context meaningfully outweigh a marginal breakout without
            being an outright veto (a base_score of 100 in the weakest
            sector still nets 60, not 0).
        neutral_sector_multiplier: Multiplier used when no SectorScore is
            available for a ticker's sector at all (e.g. sector excluded
            by sector_rotation's `min_constituents` gate).
        fallback_risk_pct: risk_reward_pct handed to final_ranker when
            breakout_engine's value is missing or NaN. Deliberately set
            far above any realistic max_stop_loss_pct so a ticker with
            unknown risk is sized to zero rather than guessed at.
    """
    default_regime: str = "SIDEWAYS"
    min_sector_multiplier: float = 0.6
    max_sector_multiplier: float = 1.4
    neutral_sector_multiplier: float = 1.0
    fallback_risk_pct: float = 999.0


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #

class IntegrationAdapter:
    """Converts per-ticker outputs from the five scoring engines into the
    TickerInputs schema final_ranker.AdaptiveRanker consumes.

    Example:
        adapter = IntegrationAdapter()
        inputs = adapter.build_ticker_inputs(
            ticker="HAL.NS", sector="DEFENCE", market_context=mc,
            sector_scores=sector_scores_by_name, rs_result=rs_results.get("HAL.NS"),
            inst_result=inst_results.get("HAL.NS"), breakout_result=breakout_results.get("HAL.NS"),
        )
    """

    def __init__(self, config: Optional[AdapterConfig] = None):
        """
        Args:
            config: Transform parameters to use. Defaults to
                AdapterConfig() if not supplied.
        """
        self.config = config or AdapterConfig()

    # -- regime translation --------------------------------------------------- #

    def translate_regime(self, regime: str) -> str:
        """Maps a market_context regime label onto a ranker_config.yaml
        regime key.

        Args:
            regime: `MarketContext.regime`, e.g. 'STRONG BULL'.

        Returns:
            One of the keys final_ranker's regimes actually define
            ('TRENDING_BULL', 'SIDEWAYS', 'BEAR'). Falls back to
            `config.default_regime` (with a warning) for any label not in
            `REGIME_TAXONOMY_MAP`, rather than passing an unrecognized
            string through and relying on AdaptiveRanker's own silent
            fallback.
        """
        mapped = REGIME_TAXONOMY_MAP.get(regime)
        if mapped is None:
            logger.warning(
                "translate_regime: unrecognized market_context regime '%s'; "
                "falling back to '%s'. Add it to REGIME_TAXONOMY_MAP if this "
                "is a legitimate new regime label.",
                regime, self.config.default_regime,
            )
            return self.config.default_regime
        logger.debug("translate_regime: '%s' -> '%s'", regime, mapped)
        return mapped

    # -- sector multiplier derivation ------------------------------------------ #

    def derive_sector_multiplier(self, sector_score: Optional[SectorScore]) -> float:
        """Derives a final_ranker sector multiplier from a SectorScore's
        cross-sectional percentile.

        Linear map: percentile 0 -> `min_sector_multiplier`, percentile 50
        -> 1.0 (neutral), percentile 100 -> `max_sector_multiplier`.

        Args:
            sector_score: This ticker's sector's SectorScore, or None if
                sector_rotation didn't score that sector at all (e.g. it
                was excluded for having too few constituents).

        Returns:
            A float multiplier, typically in
            [min_sector_multiplier, max_sector_multiplier].
        """
        if sector_score is None:
            logger.warning(
                "derive_sector_multiplier: no SectorScore available; using "
                "neutral multiplier %.2f",
                self.config.neutral_sector_multiplier,
            )
            return self.config.neutral_sector_multiplier

        cfg = self.config
        percentile = max(0.0, min(sector_score.percentile, 100.0))
        if percentile >= 50:
            span = cfg.max_sector_multiplier - 1.0
            multiplier = 1.0 + span * ((percentile - 50.0) / 50.0)
        else:
            span = 1.0 - cfg.min_sector_multiplier
            multiplier = 1.0 - span * ((50.0 - percentile) / 50.0)

        logger.debug(
            "derive_sector_multiplier: sector=%s percentile=%.1f -> multiplier=%.3f",
            sector_score.sector, percentile, multiplier,
        )
        return multiplier

    # -- per-field safe extraction --------------------------------------------- #

    def _safe_rs_fields(self, rs_result: Optional[RelativeStrengthResult]) -> RSInput:
        """Extracts RSInput from a RelativeStrengthResult, substituting a
        conservative zero (with a warning) when the result is missing or
        NaN rather than propagating NaN into final_ranker's arithmetic."""
        if rs_result is None:
            logger.warning("_safe_rs_fields: no RelativeStrengthResult; defaulting to 0.0")
            return RSInput(rs_percentile=0.0, rs_acceleration=0.0)

        percentile = rs_result.rs_percentile
        acceleration = rs_result.rs_acceleration
        if percentile != percentile:  # NaN check without importing math/numpy
            logger.warning(
                "_safe_rs_fields: NaN rs_percentile for a ticker (insufficient history); "
                "defaulting to 0.0"
            )
            percentile = 0.0
        if acceleration != acceleration:
            acceleration = 0.0
        return RSInput(rs_percentile=float(percentile), rs_acceleration=float(acceleration))

    def _safe_inst_fields(self, inst_result: Optional[InstitutionalFlowResult]) -> InstitutionalInput:
        """Extracts InstitutionalInput, substituting 0.0 (with a warning)
        for a missing result or NaN institutional_score."""
        if inst_result is None:
            logger.warning("_safe_inst_fields: no InstitutionalFlowResult; defaulting to 0.0")
            return InstitutionalInput(institutional_score=0.0)

        score = inst_result.institutional_score
        if score != score:
            logger.warning(
                "_safe_inst_fields: NaN institutional_score for a ticker; defaulting to 0.0"
            )
            score = 0.0
        return InstitutionalInput(institutional_score=float(score))

    def _safe_breakout_fields(self, breakout_result: Optional[BreakoutAnalysis]) -> BreakoutInput:
        """Extracts BreakoutInput, defaulting to a fail-closed state
        (trend_template_pass=False, confidence=0.0, risk_reward_pct set to
        `config.fallback_risk_pct` so it's rejected by any regime's
        max_stop_loss_pct) when the result is missing or its fields are
        NaN — a ticker with unknown risk should never be sized, not sized
        by accident."""
        if breakout_result is None:
            logger.warning(
                "_safe_breakout_fields: no BreakoutAnalysis; defaulting to fail-closed "
                "(trend_template_pass=False)"
            )
            return BreakoutInput(
                confidence=0.0,
                risk_reward_pct=self.config.fallback_risk_pct,
                trend_template_pass=False,
            )

        confidence = breakout_result.confidence
        risk_pct = breakout_result.risk_reward_pct
        if confidence != confidence:
            confidence = 0.0
        if risk_pct != risk_pct:
            logger.warning(
                "_safe_breakout_fields: NaN risk_reward_pct for a ticker; defaulting to "
                "fallback_risk_pct=%.1f (forces position_size to 0)",
                self.config.fallback_risk_pct,
            )
            risk_pct = self.config.fallback_risk_pct

        return BreakoutInput(
            confidence=float(confidence),
            risk_reward_pct=float(risk_pct),
            trend_template_pass=bool(breakout_result.trend_template_pass),
        )

    # -- public API ---------------------------------------------------------- #

    def build_ticker_inputs(
        self,
        ticker: str,
        sector: str,
        market_context: MarketContext,
        sector_scores: Dict[str, SectorScore],
        rs_result: Optional[RelativeStrengthResult],
        inst_result: Optional[InstitutionalFlowResult],
        breakout_result: Optional[BreakoutAnalysis],
    ) -> TickerInputs:
        """Builds one ticker's TickerInputs from that day's engine outputs.

        Args:
            ticker: Stock ticker.
            sector: This ticker's sector name (the caller resolves this,
                e.g. from config.Universe.SECTOR_MAP — this adapter stays
                decoupled from V1.0 and takes it as a plain argument).
            market_context: The day's single, shared MarketContext.
            sector_scores: sector_rotation output, keyed by sector name.
            rs_result: relative_strength output for this ticker, or None.
            inst_result: institutional_flow output for this ticker, or None.
            breakout_result: breakout_engine output for this ticker, or None.

        Returns:
            TickerInputs ready for final_ranker.AdaptiveRanker.
        """
        translated_regime = self.translate_regime(market_context.regime)
        sector_score = sector_scores.get(sector)
        sector_multiplier = self.derive_sector_multiplier(sector_score)

        return TickerInputs(
            ticker=ticker,
            market_context=MarketContextInput(
                regime=translated_regime,
                regime_score=float(market_context.score),
                confidence=float(market_context.confidence),
            ),
            sector_data=SectorInput(sector_multiplier=sector_multiplier),
            rs_data=self._safe_rs_fields(rs_result),
            inst_data=self._safe_inst_fields(inst_result),
            breakout_data=self._safe_breakout_fields(breakout_result),
        )

    def build_universe_inputs(
        self,
        tickers: Dict[str, str],
        market_context: MarketContext,
        sector_scores: Dict[str, SectorScore],
        rs_results: Dict[str, RelativeStrengthResult],
        inst_results: Dict[str, InstitutionalFlowResult],
        breakout_results: Dict[str, BreakoutAnalysis],
    ) -> Dict[str, TickerInputs]:
        """Builds TickerInputs for an entire universe in one call.

        Args:
            tickers: Mapping of ticker -> sector name for every ticker to
                build inputs for.
            market_context: The day's single, shared MarketContext.
            sector_scores: sector_rotation output, keyed by sector name.
            rs_results, inst_results, breakout_results: Per-engine output,
                keyed by ticker. A ticker missing from one of these dicts
                gets that engine's safe fallback (see `_safe_*_fields`)
                rather than raising a KeyError.

        Returns:
            Dict mapping ticker -> TickerInputs, one entry per key in
            `tickers`.
        """
        result: Dict[str, TickerInputs] = {}
        for ticker, sector in tickers.items():
            result[ticker] = self.build_ticker_inputs(
                ticker=ticker,
                sector=sector,
                market_context=market_context,
                sector_scores=sector_scores,
                rs_result=rs_results.get(ticker),
                inst_result=inst_results.get(ticker),
                breakout_result=breakout_results.get(ticker),
            )
        logger.info("build_universe_inputs: translated %d ticker(s) into TickerInputs", len(result))
        return result

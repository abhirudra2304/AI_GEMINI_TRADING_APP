"""
IAS Validation Report (IAS Plug-in Module)

Pure, read-only diagnostics over an already-completed pipeline_runner.py
run. Explains the Final Ranker's output rather than producing it: which
stocks made the Top-N Alpha Picks and why, which filter stage eliminated
every stock that didn't, how scores are distributed across the whole
scanned universe, and which results look statistically suspicious.

This module performs NO scoring, ranking, filtering, or trading logic of
its own — every number it reports was already computed by market_context,
sector_rotation, relative_strength, institutional_flow, breakout_engine,
integration_adapter, or final_ranker. It only reads, aggregates, and
narrates those results. It never touches the scanner (V1.0) or any of the
scoring engines.

Design notes
------------
- Completely independent of V1.0: no imports from config.py,
  data_broker.py, or scanner_engine.py.
- Depends on the *output types* of the six upstream IAS modules
  (MarketContext, SectorScore, RelativeStrengthResult,
  InstitutionalFlowResult, BreakoutAnalysis, TickerInputs, FinalRankResult,
  RankerConfig) purely to read already-computed fields — it never calls
  any of their calculation methods.
- All thresholds used to flag a "suspicious" result live in
  `ValidationReportConfig` — nothing hardcoded inline.
- No global/module-level mutable state.
- Every builder method (`build_top_picks`, `build_rejections`,
  `build_distributions`, `build_suspicious`) takes a `PipelineRunArtifacts`
  bundle and returns plain dataclasses, independently testable with
  hand-built fixtures — no live pipeline run required.
- Logs at INFO for the report-generation summary and DEBUG for individual
  rejection/suspicious-result determinations, via a module-level `logger`
  (standard `logging` module).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from breakout_engine import BreakoutAnalysis
from final_ranker import FinalRankResult, RankerConfig, TickerInputs
from institutional_flow import InstitutionalFlowResult
from market_context import MarketContext
from relative_strength import RelativeStrengthResult
from sector_rotation import SectorScore

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Input bundle — everything pipeline_runner.py already computed in one run
# --------------------------------------------------------------------------- #

@dataclass
class PipelineRunArtifacts:
    """Every intermediate and final result from one PipelineRunner.run()
    call, bundled for diagnostics. pipeline_runner.py populates this and
    stores it on `self.last_run`; nothing here is recomputed.

    Attributes:
        scan_date: The run's scan date, 'YYYY-MM-DD'.
        market_context: The day's MarketContext.
        ranker_config: The RankerConfig the run actually used.
        sector_scores: sector_rotation output, keyed by sector name.
        rs_results: relative_strength output, keyed by ticker.
        inst_results: institutional_flow output, keyed by ticker.
        breakout_results: breakout_engine output, keyed by ticker.
        ticker_inputs: integration_adapter output (what final_ranker
            actually consumed per ticker), keyed by ticker.
        final_results: final_ranker output, keyed by ticker.
        sector_map: ticker -> sector name.
        universe_requested: Every ticker the run was asked to scan,
            including ones later dropped for insufficient history.
        tickers_skipped_insufficient_history: Tickers dropped before any
            engine ran, with the reason already known.
    """
    scan_date: str
    market_context: MarketContext
    ranker_config: RankerConfig
    sector_scores: Dict[str, SectorScore]
    rs_results: Dict[str, RelativeStrengthResult]
    inst_results: Dict[str, InstitutionalFlowResult]
    breakout_results: Dict[str, BreakoutAnalysis]
    ticker_inputs: Dict[str, TickerInputs]
    final_results: Dict[str, FinalRankResult]
    sector_map: Dict[str, str]
    universe_requested: List[str]
    tickers_skipped_insufficient_history: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ValidationReportConfig:
    """All tunable thresholds for ValidationReportEngine.

    Attributes:
        top_n: How many Top Alpha Picks to explain in detail.
        weak_rs_threshold: RS percentile below which a pick is "weak RS".
        weak_inst_threshold: Institutional score below which a pick is
            "weak institutional flow".
        weak_breakout_threshold: Breakout confidence below which a pick is
            "weak breakout confidence".
        high_alpha_threshold: Alpha score above which a "high alpha" flag
            can trigger against any of the weak-component thresholds
            above.
        low_breakout_gate_threshold: Breakout confidence below which a
            trend-template-passing stock is flagged as having only barely
            cleared the pattern gate.
        borderline_risk_margin_pct: A stock's risk_pct within this many
            percentage points of its regime's max_stop_loss_pct is flagged
            as a borderline risk-gate pass.
        extreme_sector_multiplier_high: Sector multiplier at/above this
            value is flagged as doing outsized work in a Top-N pick.
        extreme_sector_multiplier_low: Sector multiplier at/below this
            value is flagged the same way (for a pick that made the cut
            despite it).
        distribution_percentiles: Percentiles reported in each score
            distribution.
    """
    top_n: int = 10
    weak_rs_threshold: float = 40.0
    weak_inst_threshold: float = 30.0
    weak_breakout_threshold: float = 40.0
    high_alpha_threshold: float = 70.0
    low_breakout_gate_threshold: float = 20.0
    borderline_risk_margin_pct: float = 1.0
    extreme_sector_multiplier_high: float = 1.15
    extreme_sector_multiplier_low: float = 0.85
    distribution_percentiles: Tuple[float, ...] = (0, 10, 25, 50, 75, 90, 100)
    grade_a_threshold: float = 80.0
    grade_b_threshold: float = 65.0
    grade_c_threshold: float = 50.0
    grade_d_threshold: float = 35.0
    confidence_very_high_threshold: float = 0.80
    confidence_high_threshold: float = 0.70
    confidence_moderate_threshold: float = 0.60


# --------------------------------------------------------------------------- #
# Output dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class RankExplanation:
    """Why one Top-N pick ranked where it did.

    Attributes:
        ticker: Stock ticker.
        rank: 1-based rank by alpha_score among all scored tickers.
        alpha_score, probability, position_size: As computed by
            final_ranker.
        sector: Ticker's sector.
        rs_percentile: relative_strength output for this ticker.
        sector_score: This sector's composite SectorScore.score.
        institutional_score: institutional_flow output for this ticker.
        breakout_confidence: breakout_engine confidence for this ticker.
        detected_patterns: breakout_engine patterns detected.
        dominant_factor: Which of RS / Institutional Flow / Breakout
            contributed the most to the pre-multiplier base score.
        narrative: Human-readable explanation sentence.
        system_confidence: As computed by final_ranker (0-100 blend of
            breakout confidence and market confidence) — previously
            computed but never surfaced in any report.
        grade: Letter grade (A-F) derived from alpha_score, per
            ValidationReportConfig.grade_*_threshold. Presentation only —
            does not feed back into any score.
        confidence_label: Deterministic bucket ('Very High'/'High'/
            'Moderate'/'Low') derived from probability, per
            ValidationReportConfig.confidence_*_threshold.
        conviction_stars: 1-5 filled/unfilled star string derived from
            probability.
        why_bullets: Positive factors, built only from this ticker's
            already-computed indicators (RS percentile, dominant factor,
            detected patterns, sector standing).
        risk_bullets: Warning factors from the same borderline/weak-
            component checks build_suspicious() applies, scoped to this
            ticker; ["No major technical risks detected."] if none apply.
    """
    ticker: str
    rank: int
    alpha_score: float
    probability: float
    position_size: float
    sector: str
    rs_percentile: float
    sector_score: float
    institutional_score: float
    breakout_confidence: float
    detected_patterns: List[str]
    dominant_factor: str
    narrative: str
    system_confidence: float
    grade: str
    confidence_label: str
    conviction_stars: str
    why_bullets: List[str]
    risk_bullets: List[str]


@dataclass
class RejectionReason:
    """Why one non-picked ticker didn't make the Top-N.

    Attributes:
        ticker: Stock ticker.
        stage: The pipeline stage/filter that eliminated it — one of
            'Data Fetch', 'Trend Template', 'Risk Gate', 'Not Top-N'.
        reason: Human-readable detail.
    """
    ticker: str
    stage: str
    reason: str


@dataclass
class ScoreDistribution:
    """Descriptive statistics for one score across the scanned universe.

    Attributes:
        metric: Name of the scored quantity (e.g. 'alpha_score').
        count: Number of tickers contributing a value.
        minimum, maximum, mean, std_dev: Standard summary statistics.
        percentiles: Mapping of percentile (e.g. 50.0) -> value, per
            `ValidationReportConfig.distribution_percentiles`.
    """
    metric: str
    count: int
    minimum: float
    maximum: float
    mean: float
    std_dev: float
    percentiles: Dict[float, float]


@dataclass
class SuspiciousResult:
    """One flagged anomaly worth a human's attention.

    Attributes:
        ticker: Stock ticker.
        flag: Short label, e.g. 'High Alpha / Weak RS'.
        detail: Human-readable explanation of what looks off.
    """
    ticker: str
    flag: str
    detail: str


@dataclass
class ValidationReport:
    """Complete diagnostics output for one pipeline run.

    Attributes:
        scan_date: The run's scan date.
        universe_requested: Total tickers requested for this run.
        scored_count: Tickers that made it through data fetch and were
            actually scored.
        skipped_count: Tickers dropped before scoring (insufficient
            history).
        top_picks: Up to `config.top_n` RankExplanation rows.
        rejections: One RejectionReason per non-picked ticker.
        distributions: One ScoreDistribution per tracked metric.
        suspicious: Flagged anomalies, if any.
    """
    scan_date: str
    universe_requested: int
    scored_count: int
    skipped_count: int
    top_picks: List[RankExplanation]
    rejections: List[RejectionReason]
    distributions: List[ScoreDistribution]
    suspicious: List[SuspiciousResult]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class ValidationReportEngine:
    """Builds and prints a ValidationReport from a PipelineRunArtifacts
    bundle.

    Example:
        engine = ValidationReportEngine()
        report = engine.generate_report(artifacts)
        engine.print_report(report)
    """

    _TRACKED_METRICS = ("alpha_score", "rs_percentile", "institutional_score", "breakout_confidence")

    def __init__(self, config: Optional[ValidationReportConfig] = None):
        """
        Args:
            config: Thresholds to use. Defaults to
                ValidationReportConfig() if not supplied.
        """
        self.config = config or ValidationReportConfig()
        self.console = Console()

    # -- shared helpers --------------------------------------------------------- #

    def _dominant_factor(
        self, rs_percentile: float, institutional_score: float, breakout_confidence: float, weights
    ) -> str:
        """Identifies which of RS / Institutional Flow / Breakout
        contributed the most to the pre-multiplier base score, given the
        regime's actual weights."""
        contributions = {
            "Relative Strength": rs_percentile * weights.weight_rs,
            "Institutional Flow": institutional_score * weights.weight_inst_flow,
            "Breakout Confidence": breakout_confidence * weights.weight_breakout,
        }
        return max(contributions, key=contributions.get)

    def _build_narrative(
        self,
        ticker: str,
        rank: int,
        inputs: TickerInputs,
        result: FinalRankResult,
        sector_score_value: float,
        weights,
    ) -> Tuple[str, str]:
        """Builds (dominant_factor, narrative sentence) for one pick."""
        rs = inputs.rs_data.rs_percentile
        inst = inputs.inst_data.institutional_score
        brk = inputs.breakout_data.confidence
        multiplier = inputs.sector_data.sector_multiplier
        regime = inputs.market_context.regime

        dominant = self._dominant_factor(rs, inst, brk, weights)
        base_score = rs * weights.weight_rs + inst * weights.weight_inst_flow + brk * weights.weight_breakout

        multiplier_effect = "amplified" if multiplier > 1.0 else ("dampened" if multiplier < 1.0 else "left unchanged")

        narrative = (
            f"Ranked #{rank} with Alpha Score {result.alpha_score:.1f} under a {regime} regime "
            f"(weights RS {weights.weight_rs:.0%} / Inst {weights.weight_inst_flow:.0%} / "
            f"Breakout {weights.weight_breakout:.0%}). {dominant} was the largest single contributor "
            f"to the base score of {base_score:.1f}. The sector multiplier ({multiplier:.2f}x) "
            f"{multiplier_effect} that base into the final {result.alpha_score:.1f}. "
            f"Risk gate: {result.risk_pct:.1f}% stop distance -> position size {result.position_size:.2f}."
        )
        return dominant, narrative

    def _grade(self, alpha_score: float) -> str:
        """Deterministic letter grade from alpha_score. Presentation only —
        thresholds live in config, never fed back into scoring."""
        cfg = self.config
        if alpha_score >= cfg.grade_a_threshold:
            return "A"
        if alpha_score >= cfg.grade_b_threshold:
            return "B"
        if alpha_score >= cfg.grade_c_threshold:
            return "C"
        if alpha_score >= cfg.grade_d_threshold:
            return "D"
        return "F"

    def _confidence_label(self, probability: float) -> str:
        """Deterministic probability-of-success bucket from `probability`
        (0.0-1.0, as already computed by final_ranker)."""
        cfg = self.config
        if probability >= cfg.confidence_very_high_threshold:
            return "Very High"
        if probability >= cfg.confidence_high_threshold:
            return "High"
        if probability >= cfg.confidence_moderate_threshold:
            return "Moderate"
        return "Low"

    def _conviction_stars(self, probability: float) -> str:
        """1-5 filled/unfilled star string from `probability`."""
        filled = max(1, min(5, round(probability * 5)))
        return "★" * filled + "☆" * (5 - filled)

    def _why_bullets(
        self, inputs: TickerInputs, result: FinalRankResult, dominant: str,
        sector_score_value: float, patterns: List[str],
    ) -> List[str]:
        """Builds positive bullets strictly from already-computed
        indicators for this ticker — no fabricated commentary."""
        cfg = self.config
        rs = inputs.rs_data.rs_percentile
        inst = inputs.inst_data.institutional_score
        multiplier = inputs.sector_data.sector_multiplier
        bullets = [f"Dominant factor: {dominant} (largest contributor to base score)."]

        if rs >= 100 - 5:
            bullets.append(f"Relative Strength in the top 5% (percentile {rs:.1f}).")
        elif rs >= cfg.weak_rs_threshold:
            bullets.append(f"Relative Strength percentile {rs:.1f} (above the {cfg.weak_rs_threshold:.0f} weak-RS floor).")

        if inst >= cfg.weak_inst_threshold:
            bullets.append(f"Institutional Flow score {inst:.1f} (above the {cfg.weak_inst_threshold:.0f} weak-flow floor).")

        if patterns:
            bullets.append(f"Confirmed breakout pattern(s): {', '.join(patterns)}.")

        if multiplier > 1.0:
            bullets.append(f"Sector multiplier {multiplier:.2f}x amplified the base score (sector score {sector_score_value:.1f}).")

        if result.position_size > 0:
            bullets.append(f"Risk gate passed: {result.risk_pct:.1f}% stop distance -> position size {result.position_size:.2f}.")

        return bullets

    def _risk_bullets(
        self, ticker: str, inputs: TickerInputs, result: FinalRankResult, weights,
    ) -> List[str]:
        """Builds warning bullets using the same borderline/weak-component
        checks build_suspicious() applies, scoped to one ticker. Falls
        back to an explicit 'none detected' line — never fabricates a risk
        that wasn't actually flagged."""
        cfg = self.config
        rs = inputs.rs_data.rs_percentile
        inst = inputs.inst_data.institutional_score
        brk = inputs.breakout_data.confidence
        multiplier = inputs.sector_data.sector_multiplier
        bullets: List[str] = []

        if result.alpha_score >= cfg.high_alpha_threshold and rs < cfg.weak_rs_threshold:
            bullets.append(f"⚠ High Alpha Score ({result.alpha_score:.1f}) despite weak RS percentile ({rs:.1f}).")
        if result.alpha_score >= cfg.high_alpha_threshold and inst < cfg.weak_inst_threshold:
            bullets.append(f"⚠ High Alpha Score ({result.alpha_score:.1f}) despite weak Institutional Flow ({inst:.1f}).")
        if result.alpha_score >= cfg.high_alpha_threshold and brk < cfg.weak_breakout_threshold:
            bullets.append(f"⚠ High Alpha Score ({result.alpha_score:.1f}) despite weak Breakout Confidence ({brk:.1f}).")
        if inputs.breakout_data.trend_template_pass and brk < cfg.low_breakout_gate_threshold:
            bullets.append(f"⚠ Barely cleared the pattern gate (Breakout Confidence {brk:.1f}).")
        if result.position_size > 0 and (weights.max_stop_loss_pct - result.risk_pct) <= cfg.borderline_risk_margin_pct:
            bullets.append(
                f"⚠ Borderline risk gate: {result.risk_pct:.1f}% stop distance is within "
                f"{cfg.borderline_risk_margin_pct:.1f}pt of the {weights.max_stop_loss_pct:.1f}% regime cap."
            )
        if result.alpha_score >= cfg.high_alpha_threshold and (
            multiplier >= cfg.extreme_sector_multiplier_high or multiplier <= cfg.extreme_sector_multiplier_low
        ):
            bullets.append(f"⚠ Sector multiplier ({multiplier:.2f}x) is doing outsized work in this score.")

        if not bullets:
            bullets.append("No major technical risks detected.")
        return bullets

    # -- builders --------------------------------------------------------------- #

    def build_top_picks(self, artifacts: PipelineRunArtifacts) -> List[RankExplanation]:
        """Builds explanations for the top `config.top_n` tickers by
        alpha_score.

        Args:
            artifacts: One pipeline run's full result bundle.

        Returns:
            List[RankExplanation], ranked best-first.
        """
        ranked = sorted(artifacts.final_results.values(), key=lambda r: r.alpha_score, reverse=True)
        explanations = []

        for rank, result in enumerate(ranked[: self.config.top_n], start=1):
            ticker = result.ticker
            inputs = artifacts.ticker_inputs.get(ticker)
            if inputs is None:
                logger.warning("build_top_picks: no TickerInputs for %s; skipping explanation", ticker)
                continue

            sector = artifacts.sector_map.get(ticker, "OTHER")
            sector_score_obj = artifacts.sector_scores.get(sector)
            sector_score_value = sector_score_obj.score if sector_score_obj else 0.0

            weights = artifacts.ranker_config.get_regime_weights(inputs.market_context.regime)
            dominant, narrative = self._build_narrative(ticker, rank, inputs, result, sector_score_value, weights)

            breakout = artifacts.breakout_results.get(ticker)
            patterns = breakout.detected_patterns if breakout else []

            why_bullets = self._why_bullets(inputs, result, dominant, sector_score_value, patterns)
            risk_bullets = self._risk_bullets(ticker, inputs, result, weights)

            explanations.append(
                RankExplanation(
                    ticker=ticker,
                    rank=rank,
                    alpha_score=result.alpha_score,
                    probability=result.probability,
                    position_size=result.position_size,
                    sector=sector,
                    rs_percentile=inputs.rs_data.rs_percentile,
                    sector_score=sector_score_value,
                    institutional_score=inputs.inst_data.institutional_score,
                    breakout_confidence=inputs.breakout_data.confidence,
                    detected_patterns=patterns,
                    dominant_factor=dominant,
                    narrative=narrative,
                    system_confidence=result.system_confidence,
                    grade=self._grade(result.alpha_score),
                    confidence_label=self._confidence_label(result.probability),
                    conviction_stars=self._conviction_stars(result.probability),
                    why_bullets=why_bullets,
                    risk_bullets=risk_bullets,
                )
            )

        return explanations

    def build_rejections(self, artifacts: PipelineRunArtifacts) -> List[RejectionReason]:
        """Determines, for every ticker that isn't a Top-N actionable
        pick, exactly which stage eliminated it — walking the same
        funnel the pipeline itself uses: data fetch -> trend template ->
        risk gate -> rank cutoff.

        Args:
            artifacts: One pipeline run's full result bundle.

        Returns:
            List[RejectionReason], one row per rejected ticker.
        """
        top_n_tickers = {
            r.ticker
            for r in sorted(artifacts.final_results.values(), key=lambda r: r.alpha_score, reverse=True)[
                : self.config.top_n
            ]
            if r.position_size > 0
        }

        reasons: List[RejectionReason] = []

        for ticker in artifacts.tickers_skipped_insufficient_history:
            reasons.append(
                RejectionReason(
                    ticker=ticker,
                    stage="Data Fetch",
                    reason="Insufficient price history to compute the scoring windows required (skipped before any engine ran).",
                )
            )

        for ticker in artifacts.universe_requested:
            if ticker in artifacts.tickers_skipped_insufficient_history:
                continue
            if ticker in top_n_tickers:
                continue

            result = artifacts.final_results.get(ticker)
            inputs = artifacts.ticker_inputs.get(ticker)
            if result is None or inputs is None:
                reasons.append(
                    RejectionReason(ticker=ticker, stage="Data Fetch", reason="No score was computed for this ticker.")
                )
                continue

            if not inputs.breakout_data.trend_template_pass:
                reasons.append(
                    RejectionReason(
                        ticker=ticker,
                        stage="Trend Template",
                        reason="Failed the Minervini Stage 2 Trend Template gate — no pattern/confidence is evaluated once this fails.",
                    )
                )
                continue

            weights = artifacts.ranker_config.get_regime_weights(inputs.market_context.regime)
            if result.position_size == 0.0:
                if result.risk_pct > weights.max_stop_loss_pct:
                    reasons.append(
                        RejectionReason(
                            ticker=ticker,
                            stage="Risk Gate",
                            reason=(
                                f"Risk distance {result.risk_pct:.1f}% exceeds the "
                                f"{inputs.market_context.regime} regime's max_stop_loss_pct "
                                f"({weights.max_stop_loss_pct:.1f}%)."
                            ),
                        )
                    )
                else:
                    reasons.append(
                        RejectionReason(
                            ticker=ticker,
                            stage="Risk Gate",
                            reason="Position size resolved to zero (degenerate or non-positive risk distance).",
                        )
                    )
                continue

            reasons.append(
                RejectionReason(
                    ticker=ticker,
                    stage="Not Top-N",
                    reason=(
                        f"Passed every gate with Alpha Score {result.alpha_score:.1f}, but ranked "
                        f"outside the top {self.config.top_n} by Alpha Score."
                    ),
                )
            )

        return reasons

    def build_distributions(self, artifacts: PipelineRunArtifacts) -> List[ScoreDistribution]:
        """Computes descriptive statistics for each tracked metric across
        every scored ticker in the universe (not just the Top-N).

        Args:
            artifacts: One pipeline run's full result bundle.

        Returns:
            List[ScoreDistribution], one per metric in `_TRACKED_METRICS`.
        """
        values_by_metric: Dict[str, List[float]] = {m: [] for m in self._TRACKED_METRICS}

        for ticker, result in artifacts.final_results.items():
            inputs = artifacts.ticker_inputs.get(ticker)
            if inputs is None:
                continue
            values_by_metric["alpha_score"].append(result.alpha_score)
            values_by_metric["rs_percentile"].append(inputs.rs_data.rs_percentile)
            values_by_metric["institutional_score"].append(inputs.inst_data.institutional_score)
            values_by_metric["breakout_confidence"].append(inputs.breakout_data.confidence)

        distributions = []
        for metric, values in values_by_metric.items():
            arr = np.array([v for v in values if v == v])  # drop NaN
            if arr.size == 0:
                continue
            percentiles = {p: float(np.percentile(arr, p)) for p in self.config.distribution_percentiles}
            distributions.append(
                ScoreDistribution(
                    metric=metric,
                    count=int(arr.size),
                    minimum=float(arr.min()),
                    maximum=float(arr.max()),
                    mean=float(arr.mean()),
                    std_dev=float(arr.std()),
                    percentiles=percentiles,
                )
            )
        return distributions

    def build_suspicious(self, artifacts: PipelineRunArtifacts) -> List[SuspiciousResult]:
        """Flags results that look statistically inconsistent — a high
        composite score built on a surprisingly weak underlying
        component, a pattern gate barely cleared, a borderline risk-gate
        pass, or a sector multiplier doing outsized work.

        Args:
            artifacts: One pipeline run's full result bundle.

        Returns:
            List[SuspiciousResult], possibly empty.
        """
        cfg = self.config
        flagged: List[SuspiciousResult] = []

        for ticker, result in artifacts.final_results.items():
            inputs = artifacts.ticker_inputs.get(ticker)
            if inputs is None or result.alpha_score <= 0:
                continue

            rs = inputs.rs_data.rs_percentile
            inst = inputs.inst_data.institutional_score
            brk = inputs.breakout_data.confidence
            multiplier = inputs.sector_data.sector_multiplier

            if result.alpha_score >= cfg.high_alpha_threshold and rs < cfg.weak_rs_threshold:
                flagged.append(
                    SuspiciousResult(
                        ticker=ticker,
                        flag="High Alpha / Weak RS",
                        detail=f"Alpha Score {result.alpha_score:.1f} despite RS percentile only {rs:.1f}.",
                    )
                )
            if result.alpha_score >= cfg.high_alpha_threshold and inst < cfg.weak_inst_threshold:
                flagged.append(
                    SuspiciousResult(
                        ticker=ticker,
                        flag="High Alpha / Weak Institutional Flow",
                        detail=f"Alpha Score {result.alpha_score:.1f} despite Institutional Score only {inst:.1f}.",
                    )
                )
            if result.alpha_score >= cfg.high_alpha_threshold and brk < cfg.weak_breakout_threshold:
                flagged.append(
                    SuspiciousResult(
                        ticker=ticker,
                        flag="High Alpha / Weak Breakout Confidence",
                        detail=f"Alpha Score {result.alpha_score:.1f} despite Breakout Confidence only {brk:.1f}.",
                    )
                )
            if inputs.breakout_data.trend_template_pass and brk < cfg.low_breakout_gate_threshold:
                flagged.append(
                    SuspiciousResult(
                        ticker=ticker,
                        flag="Barely Cleared Pattern Gate",
                        detail=f"Trend Template passed but Breakout Confidence is only {brk:.1f}.",
                    )
                )

            weights = artifacts.ranker_config.get_regime_weights(inputs.market_context.regime)
            if result.position_size > 0 and (weights.max_stop_loss_pct - result.risk_pct) <= cfg.borderline_risk_margin_pct:
                flagged.append(
                    SuspiciousResult(
                        ticker=ticker,
                        flag="Borderline Risk Gate",
                        detail=(
                            f"Risk distance {result.risk_pct:.1f}% is within "
                            f"{cfg.borderline_risk_margin_pct:.1f}pt of the "
                            f"{weights.max_stop_loss_pct:.1f}% regime cap."
                        ),
                    )
                )

            if (
                result.alpha_score >= cfg.high_alpha_threshold
                and (multiplier >= cfg.extreme_sector_multiplier_high or multiplier <= cfg.extreme_sector_multiplier_low)
            ):
                flagged.append(
                    SuspiciousResult(
                        ticker=ticker,
                        flag="Sector Multiplier At Extreme",
                        detail=f"Sector multiplier {multiplier:.2f}x is doing outsized work in a {result.alpha_score:.1f} Alpha Score.",
                    )
                )

        return flagged

    # -- public API ---------------------------------------------------------- #

    def generate_report(self, artifacts: PipelineRunArtifacts) -> ValidationReport:
        """Runs all four builders and assembles the full ValidationReport.

        Args:
            artifacts: One pipeline run's full result bundle.

        Returns:
            ValidationReport.
        """
        top_picks = self.build_top_picks(artifacts)
        rejections = self.build_rejections(artifacts)
        distributions = self.build_distributions(artifacts)
        suspicious = self.build_suspicious(artifacts)

        report = ValidationReport(
            scan_date=artifacts.scan_date,
            universe_requested=len(artifacts.universe_requested),
            scored_count=len(artifacts.final_results),
            skipped_count=len(artifacts.tickers_skipped_insufficient_history),
            top_picks=top_picks,
            rejections=rejections,
            distributions=distributions,
            suspicious=suspicious,
        )
        logger.info(
            "generate_report: %s — universe=%d scored=%d skipped=%d top_picks=%d suspicious=%d",
            artifacts.scan_date, report.universe_requested, report.scored_count,
            report.skipped_count, len(top_picks), len(suspicious),
        )
        return report

    def print_report(self, report: ValidationReport, market_context: Optional[MarketContext] = None) -> None:
        """Prints the full report to the terminal via Rich. Output only —
        no side effects beyond writing to the console.

        Args:
            report: The ValidationReport to print.
            market_context: Optional — if supplied, adds a Regime/VIX line
                to the header. Not required by ValidationReport itself so
                existing callers keep working unchanged.
        """
        c = self.console
        header = (
            f"[bold]Scan Date:[/bold] {report.scan_date}   "
            f"[bold]Universe:[/bold] {report.universe_requested}   "
            f"[bold]Scored:[/bold] {report.scored_count}   "
            f"[bold]Skipped (data):[/bold] {report.skipped_count}"
        )
        if market_context is not None:
            vix = market_context.volatility.vix_level
            vix_text = f"{vix:.1f}" if isinstance(vix, (int, float)) and vix == vix else "Not Available"
            header += (
                f"\n[bold]Market Regime:[/bold] {market_context.regime}   "
                f"[bold]India VIX:[/bold] {vix_text}   "
                f"[bold]Breadth:[/bold] {market_context.breadth.pct_above_50dma:.1f}% above 50-DMA"
            )
        c.print(Panel(header, title="IAS Validation Report", border_style="cyan"))

        # 1-7: Top picks
        table = Table(title=f"Top {len(report.top_picks)} Alpha Picks", expand=True)
        for col in ("Rank", "Ticker", "Alpha", "Grade", "Prob.", "Confidence", "Conviction", "RS Pctl", "Sector Score", "Inst. Score", "Pattern"):
            table.add_column(col, justify="right" if col not in ("Ticker", "Pattern", "Conviction") else "left")
        for pick in report.top_picks:
            table.add_row(
                str(pick.rank), pick.ticker, f"{pick.alpha_score:.1f}", pick.grade,
                f"{pick.probability:.2f}", f"{pick.confidence_label} ({pick.system_confidence:.0f}%)",
                pick.conviction_stars, f"{pick.rs_percentile:.1f}", f"{pick.sector_score:.1f}",
                f"{pick.institutional_score:.1f}", ", ".join(pick.detected_patterns) or "-",
            )
        c.print(table)

        for pick in report.top_picks:
            c.print(
                f"  [bold]#{pick.rank} {pick.ticker}[/bold] — "
                f"Overall Score: {pick.alpha_score:.1f} / 100 | Grade: {pick.grade} | "
                f"Confidence: {pick.system_confidence:.0f}% | Conviction: {pick.conviction_stars}"
            )
            c.print(f"    {pick.narrative}")
            c.print("    [bold]Why This Stock?[/bold]")
            for bullet in pick.why_bullets:
                c.print(f"      ✓ {bullet}")
            c.print("    [bold]Risk Factors:[/bold]")
            for bullet in pick.risk_bullets:
                c.print(f"      {bullet}")
        c.print()

        # 8: Rejections
        rej_table = Table(title=f"Rejections ({len(report.rejections)})", expand=True)
        rej_table.add_column("Ticker")
        rej_table.add_column("Filter Stage")
        rej_table.add_column("Reason")
        for r in report.rejections:
            rej_table.add_row(r.ticker, r.stage, r.reason)
        c.print(rej_table)

        # 9: Distributions
        dist_table = Table(title="Score Distribution Across Universe", expand=True)
        dist_table.add_column("Metric")
        dist_table.add_column("N", justify="right")
        dist_table.add_column("Min", justify="right")
        dist_table.add_column("P10", justify="right")
        dist_table.add_column("P25", justify="right")
        dist_table.add_column("Median", justify="right")
        dist_table.add_column("P75", justify="right")
        dist_table.add_column("P90", justify="right")
        dist_table.add_column("Max", justify="right")
        dist_table.add_column("Mean", justify="right")
        dist_table.add_column("StdDev", justify="right")
        for d in report.distributions:
            p = d.percentiles
            dist_table.add_row(
                d.metric, str(d.count), f"{d.minimum:.1f}", f"{p.get(10, float('nan')):.1f}",
                f"{p.get(25, float('nan')):.1f}", f"{p.get(50, float('nan')):.1f}",
                f"{p.get(75, float('nan')):.1f}", f"{p.get(90, float('nan')):.1f}",
                f"{d.maximum:.1f}", f"{d.mean:.1f}", f"{d.std_dev:.1f}",
            )
        c.print(dist_table)

        # 10: Suspicious results
        if report.suspicious:
            susp_table = Table(title=f"Suspicious Results ({len(report.suspicious)})", expand=True, border_style="yellow")
            susp_table.add_column("Ticker")
            susp_table.add_column("Flag")
            susp_table.add_column("Detail")
            for s in report.suspicious:
                susp_table.add_row(s.ticker, s.flag, s.detail)
            c.print(susp_table)
        else:
            c.print(Panel(Text("No suspicious results flagged.", style="dim"), border_style="green"))

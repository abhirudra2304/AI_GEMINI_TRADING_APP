"""
Weekly System Review Engine (IAS Plug-in Module)

Read-only performance analytics: pulls historical scan outcomes from
analytics_repository.py, computes classification metrics (hit rate,
precision, recall) and per-factor Information Coefficient, and produces a
human-readable, human-*reviewed* recommendation for factor weight
adjustments. Never writes to the database and never touches
ranker_config.yaml — recommendations are output only.

Design notes
------------
- Pure reporting module: no calculation results are ever written back
  anywhere. `generate_weekly_report()` only reads (via
  `AnalyticsRepository.fetch_training_data`, itself a read-only SELECT)
  and returns/prints a `WeeklyReviewReport`. Applying a recommended
  adjustment to ranker_config.yaml is a deliberate, separate, human action
  — this module cannot perform it, by design.
- Only two intentional dependencies on other IAS modules, both read-only:
  `analytics_repository.AnalyticsRepository` (to fetch historical scan
  rows) and `final_ranker.RankerConfig` (to read the *current* regime
  weights, needed so recommended deltas can be expressed relative to them
  and kept net-zero-sum). Neither is mutated here. No dependency on
  config.py, data_broker.py, scanner_engine.py, or any Version 1.0 module.
- All thresholds and adjustment bounds live in `ReviewConfig` — nothing
  hardcoded inline.
- No global/module-level mutable state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from analytics_repository import AnalyticsRepository
from final_ranker import RankerConfig


# --------------------------------------------------------------------------- #
# Output dataclass
# --------------------------------------------------------------------------- #

@dataclass
class WeeklyReviewReport:
    """Result of one `SystemReviewEngine.generate_weekly_report()` run.

    Attributes:
        start_date: Inclusive start of the review window, 'YYYY-MM-DD'.
        end_date: Inclusive end of the review window, 'YYYY-MM-DD'.
        total_signals: Number of scan rows in the window with populated
            forward-return data (the rows this report is based on).
        hit_rate: True Positives / (True Positives + False Positives).
        precision: True Positives / total is_selected==1 rows.
        recall: True Positives / (True Positives + False Negatives).
        false_positives: Raw count of selected signals that hit their
            stop (the `fp` count behind `hit_rate`/`precision`).
        missed_winners: Raw count of non-selected candidates whose
            `ret_10d` cleared `missed_winner_threshold_pct` (the `fn`
            count behind `recall`).
        best_factor: Factor with the highest positive correlation
            (Information Coefficient) to `ret_10d`.
        worst_factor: Factor with the most negative correlation to
            `ret_10d`, or — if no factor is negatively correlated — the
            factor with the highest average score among False Positives.
        recommended_adjustments: Proposed regime weight-key deltas (e.g.
            {'weight_rs': 0.05, 'weight_breakout': -0.05}) for the human
            trader to review and, if agreed, manually apply to
            ranker_config.yaml. Always nets to 0.0 so the regime's weights
            still sum to 100% if applied as-is. Empty when the data
            doesn't support a confident recommendation.
        review_summary: Plain-text paragraph explaining the metrics and
            the reasoning behind (or absence of) the recommendation.
    """
    start_date: str
    end_date: str
    total_signals: int
    hit_rate: float
    precision: float
    recall: float
    false_positives: int
    missed_winners: int
    best_factor: str
    worst_factor: str
    recommended_adjustments: Dict[str, float]
    review_summary: str


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReviewConfig:
    """All tunable thresholds for SystemReviewEngine.

    Attributes:
        default_lookback_days: Calendar days back from `end_date` the
            review window spans when `generate_weekly_report` isn't given
            explicit dates.
        missed_winner_threshold_pct: `ret_10d` a non-selected stock must
            clear to count as a False Negative ("missed winner").
        adjustment_min_pct: Smallest weight delta this module will ever
            recommend for a factor it decides to adjust.
        adjustment_max_pct: Largest such delta.
        significance_margin: A factor's mean score among False
            Negatives (for a boost) or False Positives (for a cut) must
            exceed the dataset-wide mean by at least this many points for
            the corresponding adjustment to be recommended at all.
    """
    default_lookback_days: int = 10
    missed_winner_threshold_pct: float = 15.0
    adjustment_min_pct: float = 0.02
    adjustment_max_pct: float = 0.05
    significance_margin: float = 5.0


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class SystemReviewEngine:
    """Reads historical scan/outcome data and produces a
    WeeklyReviewReport. Performs no writes of any kind.

    Example:
        repo = AnalyticsRepository("analytics.db")
        engine = SystemReviewEngine(repo)
        report = engine.generate_weekly_report()
    """

    # Only these three factors map to a summed regime weight in
    # ranker_config.yaml (weight_rs + weight_inst_flow + weight_breakout
    # == 1.0 per regime). sector_score is deliberately excluded: it drives
    # final_ranker's sector *multiplier*, not one of the summed weights,
    # so it's still analyzed for Information Coefficient reporting but is
    # never a target for a weight-sum-preserving adjustment.
    _FACTOR_TO_WEIGHT_KEY = {
        "rs_score": "weight_rs",
        "inst_score": "weight_inst_flow",
        "breakout_score": "weight_breakout",
    }
    _FACTOR_COLUMNS = ("sector_score", "rs_score", "inst_score", "breakout_score")

    def __init__(
        self,
        repository: AnalyticsRepository,
        ranker_config: Optional[RankerConfig] = None,
        config: Optional[ReviewConfig] = None,
    ):
        """
        Args:
            repository: Read-only data source for historical scan rows.
            ranker_config: Current regime weights, used only to express
                recommended deltas relative to them. Defaults to
                `RankerConfig.load()`.
            config: Thresholds to use. Defaults to ReviewConfig().
        """
        self.repository = repository
        self.ranker_config = ranker_config or RankerConfig.load()
        self.config = config or ReviewConfig()

    # -- data loading --------------------------------------------------------- #

    def _resolve_window(self, start_date: Optional[str], end_date: Optional[str]) -> Tuple[str, str]:
        """Resolves the review window, defaulting `end_date` to today and
        `start_date` to `config.default_lookback_days` before it."""
        resolved_end = end_date or datetime.now().strftime("%Y-%m-%d")
        if start_date:
            resolved_start = start_date
        else:
            end_dt = datetime.strptime(resolved_end, "%Y-%m-%d")
            resolved_start = (end_dt - timedelta(days=self.config.default_lookback_days)).strftime(
                "%Y-%m-%d"
            )
        return resolved_start, resolved_end

    def _load_dataset(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetches scan rows for the window and keeps only those with
        forward returns already backfilled (`ret_10d`, `stop_hit`,
        `target_hit` all populated), per the Input Data Specification."""
        rows = self.repository.fetch_training_data(start_date, end_date)
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        return df.dropna(subset=["ret_10d", "stop_hit", "target_hit"])

    # -- classification -------------------------------------------------------- #

    def _classify(self, df: pd.DataFrame) -> Dict[str, pd.Series]:
        """Boolean masks for True Positives, False Positives and False
        Negatives, per the Trade Classification definitions."""
        cfg = self.config
        is_selected = df["is_selected"].astype(bool)

        true_positive = is_selected & (df["target_hit"].astype(bool))
        false_positive = is_selected & (df["stop_hit"].astype(bool))
        false_negative = (~is_selected) & (df["ret_10d"] > cfg.missed_winner_threshold_pct)

        return {"tp": true_positive, "fp": false_positive, "fn": false_negative}

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        return float(numerator) / float(denominator) if denominator else 0.0

    def calculate_classification_metrics(self, df: pd.DataFrame) -> Dict[str, float]:
        """Hit Rate, Precision and Recall for the dataset.

        Args:
            df: Outcome-populated scan rows.

        Returns:
            Dict with keys 'hit_rate', 'precision', 'recall', plus raw
            counts 'tp', 'fp', 'fn', 'total_selected'.
        """
        masks = self._classify(df)
        tp = int(masks["tp"].sum())
        fp = int(masks["fp"].sum())
        fn = int(masks["fn"].sum())
        total_selected = int(df["is_selected"].astype(bool).sum())

        return {
            "hit_rate": self._safe_ratio(tp, tp + fp),
            "precision": self._safe_ratio(tp, total_selected),
            "recall": self._safe_ratio(tp, tp + fn),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "total_selected": total_selected,
        }

    # -- factor performance ---------------------------------------------------- #

    def calculate_information_coefficients(self, df: pd.DataFrame) -> pd.Series:
        """Pearson correlation of each factor score column against
        `ret_10d` (the Information Coefficient).

        Args:
            df: Outcome-populated scan rows.

        Returns:
            pd.Series indexed by factor column name.
        """
        factor_df = df[list(self._FACTOR_COLUMNS)]
        return factor_df.corrwith(df["ret_10d"])

    def identify_best_and_worst_factors(
        self, df: pd.DataFrame, ic: pd.Series, masks: Dict[str, pd.Series]
    ) -> Tuple[str, str]:
        """Best factor = highest positive IC. Worst factor = most negative
        IC, or — if no factor is negatively correlated — the factor with
        the highest mean score among False Positives.

        Args:
            df: Outcome-populated scan rows.
            ic: As returned by `calculate_information_coefficients`.
            masks: As returned by `_classify`.

        Returns:
            (best_factor, worst_factor) column names.
        """
        best_factor = str(ic.idxmax())

        if ic.min() < 0:
            worst_factor = str(ic.idxmin())
        else:
            fp_rows = df.loc[masks["fp"], list(self._FACTOR_COLUMNS)]
            if not fp_rows.empty:
                worst_factor = str(fp_rows.mean().idxmax())
            else:
                worst_factor = str(ic.idxmin())

        return best_factor, worst_factor

    # -- recommendation heuristic ---------------------------------------------- #

    def _adjustment_magnitude(self, ic_value: float) -> float:
        """Scales a weight delta between `adjustment_min_pct` and
        `adjustment_max_pct` based on IC strength (|IC| clipped to
        [0, 1])."""
        cfg = self.config
        strength = min(abs(float(ic_value)) if pd.notna(ic_value) else 0.0, 1.0)
        return cfg.adjustment_min_pct + (cfg.adjustment_max_pct - cfg.adjustment_min_pct) * strength

    def recommend_weight_adjustments(
        self,
        df: pd.DataFrame,
        ic: pd.Series,
        masks: Dict[str, pd.Series],
        best_factor: str,
        worst_factor: str,
    ) -> Dict[str, float]:
        """Applies the Weight Adjustment Heuristic: boost `best_factor`'s
        weight if it was notably present among missed winners (False
        Negatives), cut `worst_factor`'s weight if it notably drove False
        Positives. Any change is offset elsewhere so the recommended
        deltas always sum to 0.0 (i.e. the regime's weights still sum to
        100% if applied as-is).

        Args:
            df: Outcome-populated scan rows.
            ic: Information Coefficients, as returned by
                `calculate_information_coefficients`.
            masks: As returned by `_classify`.
            best_factor, worst_factor: As returned by
                `identify_best_and_worst_factors`.

        Returns:
            Dict mapping weight key (e.g. 'weight_rs') -> recommended
            delta. Empty if no adjustment is warranted.
        """
        cfg = self.config
        overall_means = df[list(self._FACTOR_COLUMNS)].mean()

        fn_rows = df.loc[masks["fn"], list(self._FACTOR_COLUMNS)]
        should_boost = (
            best_factor in self._FACTOR_TO_WEIGHT_KEY
            and not fn_rows.empty
            and fn_rows[best_factor].mean() > overall_means[best_factor] + cfg.significance_margin
        )

        fp_rows = df.loc[masks["fp"], list(self._FACTOR_COLUMNS)]
        should_cut = (
            worst_factor in self._FACTOR_TO_WEIGHT_KEY
            and not fp_rows.empty
            and fp_rows[worst_factor].mean() > overall_means[worst_factor] + cfg.significance_margin
            and worst_factor != best_factor
        )

        if not should_boost and not should_cut:
            return {}

        increase_key = self._FACTOR_TO_WEIGHT_KEY.get(best_factor) if should_boost else None
        decrease_key = self._FACTOR_TO_WEIGHT_KEY.get(worst_factor) if should_cut else None

        magnitude = self._adjustment_magnitude(
            ic[best_factor] if should_boost else ic[worst_factor]
        )

        all_weight_keys = set(self._FACTOR_TO_WEIGHT_KEY.values())
        adjustments: Dict[str, float] = {key: 0.0 for key in all_weight_keys}

        if increase_key and decrease_key:
            adjustments[increase_key] += magnitude
            adjustments[decrease_key] -= magnitude
        elif increase_key:
            adjustments[increase_key] += magnitude
            offset_keys = sorted(all_weight_keys - {increase_key})
            for key in offset_keys:
                adjustments[key] -= magnitude / len(offset_keys)
        elif decrease_key:
            adjustments[decrease_key] -= magnitude
            offset_keys = sorted(all_weight_keys - {decrease_key})
            for key in offset_keys:
                adjustments[key] += magnitude / len(offset_keys)

        return {key: round(value, 4) for key, value in adjustments.items() if abs(value) > 1e-9}

    # -- summary text ------------------------------------------------------------ #

    def _build_summary(
        self,
        start_date: str,
        end_date: str,
        metrics: Dict[str, float],
        ic: pd.Series,
        best_factor: str,
        worst_factor: str,
        adjustments: Dict[str, float],
    ) -> str:
        """Builds the plain-text explanation paragraph for the report."""
        parts = [
            f"Reviewed {metrics['total_selected']} selected signals and "
            f"{metrics['tp'] + metrics['fn']} qualifying non-selected candidates "
            f"between {start_date} and {end_date}.",
            f"Hit rate was {metrics['hit_rate']:.1%} ({metrics['tp']} winners vs "
            f"{metrics['fp']} stop-outs), precision {metrics['precision']:.1%}, and "
            f"recall {metrics['recall']:.1%} ({metrics['fn']} missed winners not selected).",
            f"'{best_factor}' had the strongest Information Coefficient "
            f"({ic[best_factor]:.3f}) against 10-day forward returns, while "
            f"'{worst_factor}' had the weakest ({ic[worst_factor]:.3f}).",
        ]

        if adjustments:
            deltas_text = ", ".join(
                f"{key} {value:+.1%}" for key, value in sorted(adjustments.items())
            )
            parts.append(
                f"Recommended adjustment for human review: {deltas_text}. This keeps the "
                "regime's weights summing to 100% and should not be applied automatically."
            )
        else:
            parts.append(
                "No factor cleared the significance margin for a confident weight "
                "adjustment recommendation this period; current weights are left unchanged."
            )

        return " ".join(parts)

    # -- public API --------------------------------------------------------------- #

    def generate_weekly_report(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> WeeklyReviewReport:
        """Runs the full read-only review pipeline and prints the report.

        Args:
            start_date: Inclusive window start, 'YYYY-MM-DD'. Defaults to
                `config.default_lookback_days` before `end_date`.
            end_date: Inclusive window end, 'YYYY-MM-DD'. Defaults to
                today.

        Returns:
            WeeklyReviewReport for the resolved window.
        """
        resolved_start, resolved_end = self._resolve_window(start_date, end_date)
        df = self._load_dataset(resolved_start, resolved_end)

        if df.empty or len(df) < 2:
            report = WeeklyReviewReport(
                start_date=resolved_start,
                end_date=resolved_end,
                total_signals=len(df),
                hit_rate=0.0,
                precision=0.0,
                recall=0.0,
                false_positives=0,
                missed_winners=0,
                best_factor="",
                worst_factor="",
                recommended_adjustments={},
                review_summary=(
                    f"Insufficient outcome-populated scan data between {resolved_start} and "
                    f"{resolved_end} to produce a review (found {len(df)} qualifying rows). "
                    "Backfill forward returns via analytics_repository.update_forward_returns "
                    "and re-run once more history is available."
                ),
            )
            self._print_report(report)
            return report

        metrics = self.calculate_classification_metrics(df)
        ic = self.calculate_information_coefficients(df)
        masks = self._classify(df)
        best_factor, worst_factor = self.identify_best_and_worst_factors(df, ic, masks)
        adjustments = self.recommend_weight_adjustments(df, ic, masks, best_factor, worst_factor)
        summary = self._build_summary(
            resolved_start, resolved_end, metrics, ic, best_factor, worst_factor, adjustments
        )

        report = WeeklyReviewReport(
            start_date=resolved_start,
            end_date=resolved_end,
            total_signals=len(df),
            hit_rate=metrics["hit_rate"],
            precision=metrics["precision"],
            recall=metrics["recall"],
            false_positives=int(metrics["fp"]),
            missed_winners=int(metrics["fn"]),
            best_factor=best_factor,
            worst_factor=worst_factor,
            recommended_adjustments=adjustments,
            review_summary=summary,
        )
        self._print_report(report)
        return report

    @staticmethod
    def _print_report(report: WeeklyReviewReport) -> None:
        """Prints a clean console report. Output only — no side effects."""
        print("\n" + "=" * 60)
        print("WEEKLY SYSTEM REVIEW")
        print("=" * 60)
        print(f"Window            : {report.start_date} -> {report.end_date}")
        print(f"Signals Reviewed  : {report.total_signals}")
        print(f"Hit Rate          : {report.hit_rate:.1%}")
        print(f"Precision         : {report.precision:.1%}")
        print(f"Recall            : {report.recall:.1%}")
        print(f"Best Factor       : {report.best_factor}")
        print(f"Worst Factor      : {report.worst_factor}")
        print("-" * 60)
        if report.recommended_adjustments:
            print("Recommended Adjustments (for human review only):")
            for key, value in sorted(report.recommended_adjustments.items()):
                print(f"  {key:<20}: {value:+.2%}")
        else:
            print("Recommended Adjustments: none")
        print("-" * 60)
        print(report.review_summary)
        print("=" * 60)

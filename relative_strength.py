"""
Institutional Composite Relative Strength Engine (IAS Plug-in Module)

Vectorized Composite Relative Strength across a universe of stocks, driven
entirely by Pandas/NumPy whole-DataFrame operations (no per-ticker Python
loops in the calculation path).

Composite weighting:
    40% 3-month ROC (63 trading days)
    20% 6-month ROC (126 trading days)
    20% 9-month ROC (189 trading days)
    20% 12-month ROC (252 trading days)

Design notes
------------
- Completely independent: no imports from config.py, data_broker.py,
  scanner_engine.py or any other existing module, and nothing in the
  existing codebase imports this one. Adding it carries zero regression
  risk to Version 1.0.
- No global/module-level mutable state. All state lives on an instance of
  RelativeStrengthEngine (just its `config`), passed explicitly by the
  caller.
- Calculation methods (`calculate_roc`, `calculate_composite`,
  `calculate_percentile_rank`, `calculate_acceleration`,
  `calculate_persistence`) operate on the full wide DataFrame at once via
  Pandas vectorized ops — they never iterate over rows or the ticker list.
  The only per-ticker loop in the module is the final step that packages
  each column's latest values into its own RelativeStrengthResult object,
  which is unavoidable when the return type is one dataclass per ticker.
- Logs at DEBUG for pipeline stage shapes and INFO for the final ranked
  universe size, via a module-level `logger` (standard `logging` module).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Output dataclass
# --------------------------------------------------------------------------- #

@dataclass
class RelativeStrengthResult:
    """Latest Relative Strength reading for a single ticker.

    Attributes:
        ticker: Stock ticker (DataFrame column name, e.g. 'HAL.NS').
        composite_score: Weighted-ROC composite score (raw %). NaN if the
            ticker doesn't have enough history to compute all four ROC
            components on the latest trading day.
        rs_rank: 1-based rank by composite_score among tickers with a
            valid score on the latest day (1 = highest). None when
            composite_score is NaN.
        rs_percentile: Percentile ranking (1.0-99.0) of composite_score
            among tickers with a valid score on the latest day. NaN when
            composite_score is NaN.
        rs_persistence: % of the last 20 trading days (0.0-100.0) where
            the ticker's RS percentile was >= 80.0.
        rs_acceleration: Current RS percentile minus the RS percentile
            from 20 trading days ago. NaN if 20 days of prior percentile
            history isn't available.
    """
    ticker: str
    composite_score: float
    rs_rank: Optional[int]
    rs_percentile: float
    rs_persistence: float
    rs_acceleration: float


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RelativeStrengthConfig:
    """Lookback windows, weights and thresholds for RelativeStrengthEngine.

    Attributes:
        days_3m, days_6m, days_9m, days_12m: Trading-session lookbacks.
        weight_3m, weight_6m, weight_9m, weight_12m: Composite weights,
            applied to the correspondingly named ROC.
        percentile_floor: Lower clip bound for RS Percentile.
        percentile_cap: Upper clip bound for RS Percentile.
        persistence_window_days: Trailing session window examined for
            RS Persistence.
        persistence_percentile_threshold: RS percentile a ticker must be
            at/above on a given session for that session to count toward
            RS Persistence.
        acceleration_lookback_days: Sessions back used as the baseline for
            RS Acceleration.
    """
    days_3m: int = 63
    days_6m: int = 126
    days_9m: int = 189
    days_12m: int = 252

    weight_3m: float = 0.40
    weight_6m: float = 0.20
    weight_9m: float = 0.20
    weight_12m: float = 0.20

    percentile_floor: float = 1.0
    percentile_cap: float = 99.0

    persistence_window_days: int = 20
    persistence_percentile_threshold: float = 80.0

    acceleration_lookback_days: int = 20


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class RelativeStrengthEngine:
    """Vectorized calculator that turns a wide daily-close-price DataFrame
    into a per-ticker dict of RelativeStrengthResult.

    Example:
        engine = RelativeStrengthEngine()
        results = engine.calculate(prices_df)
        results['HAL.NS'].rs_percentile
    """

    def __init__(self, config: Optional[RelativeStrengthConfig] = None):
        """
        Args:
            config: Lookbacks/weights/thresholds to use. Defaults to
                RelativeStrengthConfig() if not supplied.
        """
        self.config = config or RelativeStrengthConfig()

    # -- vectorized calculators ------------------------------------------- #

    def calculate_roc(self, df: pd.DataFrame, days: int) -> pd.DataFrame:
        """Rate-of-change (%) for every ticker at every date:
        ((Price_today / Price_N_days_ago) - 1) * 100.

        Vectorized across the whole DataFrame — no per-column loop.

        Args:
            df: Wide price DataFrame (DatetimeIndex x tickers).
            days: Lookback window in trading sessions.

        Returns:
            DataFrame aligned to `df`, NaN wherever the prior price is
            missing, zero or negative (guards divide-by-zero / bad data
            without raising).
        """
        prior = df.shift(days)
        safe_prior = prior.where(prior > 0, other=np.nan)
        return (df / safe_prior - 1.0) * 100.0

    def calculate_composite(self, df: pd.DataFrame) -> pd.DataFrame:
        """Composite RS score at every date for every ticker:
        0.40*3M_ROC + 0.20*6M_ROC + 0.20*9M_ROC + 0.20*12M_ROC.

        A ticker/date with fewer than `days_12m` sessions of history will
        have NaN 12M ROC and therefore a NaN composite on that date — this
        is intentional: a partial-timeframe estimate would silently
        overstate confidence, and downstream ranking/percentile
        calculations already skip NaNs rather than letting them corrupt
        the ranking of tickers with complete history.

        Args:
            df: Wide price DataFrame (DatetimeIndex x tickers).

        Returns:
            DataFrame aligned to `df`.
        """
        cfg = self.config
        roc_3m = self.calculate_roc(df, cfg.days_3m)
        roc_6m = self.calculate_roc(df, cfg.days_6m)
        roc_9m = self.calculate_roc(df, cfg.days_9m)
        roc_12m = self.calculate_roc(df, cfg.days_12m)

        return (
            cfg.weight_3m * roc_3m
            + cfg.weight_6m * roc_6m
            + cfg.weight_9m * roc_9m
            + cfg.weight_12m * roc_12m
        )

    def calculate_rank(self, composite: pd.DataFrame) -> pd.DataFrame:
        """1-based rank per date, 1 = highest composite score. NaN
        composites remain NaN (excluded from ranking) rather than being
        assigned a rank.

        Args:
            composite: Composite score DataFrame, as returned by
                `calculate_composite`.
        """
        return composite.rank(axis=1, ascending=False, method="min")

    def calculate_percentile_rank(self, composite: pd.DataFrame) -> pd.DataFrame:
        """Percentile ranking per date, clipped to
        [config.percentile_floor, config.percentile_cap]. NaN composites
        remain NaN.

        Args:
            composite: Composite score DataFrame, as returned by
                `calculate_composite`.
        """
        cfg = self.config
        pct = composite.rank(axis=1, pct=True) * 100.0
        return pct.clip(lower=cfg.percentile_floor, upper=cfg.percentile_cap)

    def calculate_acceleration(self, percentile: pd.DataFrame) -> pd.DataFrame:
        """RS Acceleration per date: current RS percentile minus the RS
        percentile `acceleration_lookback_days` sessions earlier.

        Args:
            percentile: Percentile DataFrame, as returned by
                `calculate_percentile_rank`.
        """
        return percentile - percentile.shift(self.config.acceleration_lookback_days)

    def calculate_persistence(self, percentile: pd.DataFrame) -> pd.Series:
        """RS Persistence: % of the last `persistence_window_days`
        sessions where each ticker's RS percentile was >=
        `persistence_percentile_threshold`.

        Args:
            percentile: Percentile DataFrame, as returned by
                `calculate_percentile_rank`.

        Returns:
            pd.Series indexed by ticker, values 0.0-100.0.
        """
        cfg = self.config
        window = percentile.tail(cfg.persistence_window_days)
        meets_threshold = window >= cfg.persistence_percentile_threshold
        return meets_threshold.mean(axis=0) * 100.0

    # -- public API ---------------------------------------------------------- #

    def calculate(self, df: pd.DataFrame) -> Dict[str, RelativeStrengthResult]:
        """Runs the full vectorized pipeline and packages the latest
        state for every ticker.

        Args:
            df: Wide DataFrame of daily adjusted close prices — DatetimeIndex
                (sorted ascending), one column per ticker (e.g. 'HAL.NS',
                'BEL.NS').

        Returns:
            Dict mapping ticker -> RelativeStrengthResult for every column
            in `df`. Tickers with insufficient history to produce a valid
            composite score on the latest day still get an entry, with
            composite_score/rs_percentile/rs_acceleration as NaN and
            rs_rank as None, rather than being dropped or raising.
        """
        if df is None or df.empty:
            logger.warning("RelativeStrengthEngine.calculate: received empty/None DataFrame")
            return {}

        df = df.sort_index()
        logger.debug("RelativeStrengthEngine.calculate: %d tickers, %d sessions", df.shape[1], df.shape[0])

        composite = self.calculate_composite(df)
        rank = self.calculate_rank(composite)
        percentile = self.calculate_percentile_rank(composite)
        acceleration = self.calculate_acceleration(percentile)
        persistence = self.calculate_persistence(percentile)

        latest_date = df.index[-1]
        latest_composite = composite.loc[latest_date]
        latest_rank = rank.loc[latest_date]
        latest_percentile = percentile.loc[latest_date]
        latest_acceleration = acceleration.loc[latest_date]

        results: Dict[str, RelativeStrengthResult] = {}
        for ticker in df.columns:
            score = latest_composite[ticker]
            rank_val = latest_rank[ticker]
            pct_val = latest_percentile[ticker]
            accel_val = latest_acceleration[ticker]
            persist_val = persistence[ticker]

            results[ticker] = RelativeStrengthResult(
                ticker=ticker,
                composite_score=float(score) if pd.notna(score) else np.nan,
                rs_rank=int(rank_val) if pd.notna(rank_val) else None,
                rs_percentile=float(pct_val) if pd.notna(pct_val) else np.nan,
                rs_persistence=float(persist_val) if pd.notna(persist_val) else 0.0,
                rs_acceleration=float(accel_val) if pd.notna(accel_val) else np.nan,
            )

        valid_count = sum(1 for r in results.values() if r.rs_rank is not None)
        logger.info(
            "RelativeStrengthEngine.calculate: ranked %d/%d ticker(s) with a valid composite score",
            valid_count, len(results),
        )
        return results

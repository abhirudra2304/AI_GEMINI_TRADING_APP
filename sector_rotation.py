"""
Institutional Sector Rotation Engine (IAS Plug-in Module)

Ranks sectors against one another using four pillars — Relative Strength,
Momentum, Breadth and Volume — computed from stock-level (constituent)
data supplied by the caller.

Design notes
------------
- Completely independent: no imports from config.py, data_broker.py,
  scanner_engine.py or any other existing module.
- UPDATE 2026-08-21: `momentum_scanner.py` and `emfb.py` now import
  `print_rs_only_rotation_report` (bottom of this file) to print a
  sector-rotation section after their main ranking table - both call it
  wrapped in try/except so a failure here can never break their report.
  This is still one-directional (this file imports nothing from either of
  them) - only the "nothing imports this one" half of the claim above is
  now stale; the zero-regression-risk property still holds.
- All thresholds and weights live in `SectorRotationConfig`, a dataclass
  passed into the engine — nothing is hardcoded inline and nothing is a
  module-level constant, so callers can retune scoring without touching
  this file.
- All public types are frozen dataclasses.
- No global/module-level mutable state. All state lives on instances of
  SectorRotationEngine (just its `config`), passed explicitly by the
  caller.
- Every calculation method takes explicit arguments and returns a plain
  value or dataclass, so each is independently unit testable without
  needing live market data.
- Logs at DEBUG for per-sector pillar scores and INFO for the final
  ranking summary, via a module-level `logger` (standard `logging`
  module); no handlers/levels are configured here, that's the caller's
  responsibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Input dataclasses
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SectorConstituent:
    """One stock's contribution to its sector's rotation score.

    Attributes:
        symbol: Ticker symbol.
        sector: Sector name this stock belongs to.
        rs_percentile: Relative strength percentile (0-100) vs. a benchmark
            (e.g. Nifty 500), as already computed upstream.
        momentum_pct: Price return (%) over the lookback window used by the
            caller (e.g. 20-session return).
        above_key_ma: Whether the stock trades above its key moving average
            (e.g. 50-DMA) — the per-stock input to the sector breadth
            calculation.
        volume_ratio: Latest traded volume / trailing average volume.
            None when unavailable.
    """
    symbol: str
    sector: str
    rs_percentile: float
    momentum_pct: float
    above_key_ma: bool
    volume_ratio: Optional[float] = None


# --------------------------------------------------------------------------- #
# Output dataclass
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SectorScore:
    """Composite rotation reading for a single sector.

    Attributes:
        sector: Sector name.
        score: Composite 0-100 rotation score (weighted blend of the four
            pillars).
        percentile: This sector's `score` expressed as a percentile
            (0-100) relative to every other sector ranked in the same
            `rank_sectors()` call — 100 means strongest sector that run.
        momentum: 0-100 normalized momentum sub-score.
        breadth: 0-100 breadth sub-score (% of constituents above their
            key moving average).
        relative_strength: 0-100 relative strength sub-score (mean RS
            percentile of constituents).
        volume: 0-100 normalized volume-participation sub-score.
        constituent_count: Number of constituents that contributed to this
            sector's score.
    """
    sector: str
    score: float
    percentile: float
    momentum: float
    breadth: float
    relative_strength: float
    volume: float
    constituent_count: int


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SectorRotationConfig:
    """All tunable weights and thresholds for SectorRotationEngine.

    Attributes:
        weight_relative_strength: Weight of the RS pillar in the composite.
        weight_momentum: Weight of the momentum pillar.
        weight_breadth: Weight of the breadth pillar.
        weight_volume: Weight of the volume pillar.
        momentum_floor_pct: Momentum return (%) mapped to a 0 sub-score.
        momentum_cap_pct: Momentum return (%) mapped to a 100 sub-score.
            Values are linearly interpolated between floor and cap.
        volume_ratio_floor: Volume ratio mapped to a 0 sub-score.
        volume_ratio_cap: Volume ratio mapped to a 100 sub-score.
        min_constituents: Minimum number of constituents a sector must have
            to receive a score; sectors below this are excluded from
            ranking output rather than scored on thin data.
    """
    weight_relative_strength: float = 35.0
    weight_momentum: float = 30.0
    weight_breadth: float = 20.0
    weight_volume: float = 15.0

    momentum_floor_pct: float = -10.0
    momentum_cap_pct: float = 10.0

    volume_ratio_floor: float = 0.0
    volume_ratio_cap: float = 2.0

    min_constituents: int = 2

    @property
    def weights_sum(self) -> float:
        return (
            self.weight_relative_strength
            + self.weight_momentum
            + self.weight_breadth
            + self.weight_volume
        )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class SectorRotationEngine:
    """Stateless-by-default calculator that turns a flat list of
    SectorConstituent rows into ranked SectorScore output.

    The engine holds no global state — only its `config` — so multiple
    engines (e.g. different lookback windows or weight profiles) can run
    side by side without interfering with one another.

    Example:
        engine = SectorRotationEngine(SectorRotationConfig())
        scores = engine.rank_sectors(constituents)
    """

    def __init__(self, config: Optional[SectorRotationConfig] = None):
        """
        Args:
            config: Weights/thresholds to use. Defaults to
                SectorRotationConfig() if not supplied.
        """
        self.config = config or SectorRotationConfig()

    # -- pillar calculators ------------------------------------------------ #

    def calculate_relative_strength(self, constituents: List[SectorConstituent]) -> float:
        """Mean RS percentile of the sector's constituents, 0-100.

        Args:
            constituents: Non-empty list of stocks belonging to one sector.

        Returns:
            0.0 if `constituents` is empty.
        """
        if not constituents:
            return 0.0
        values = [c.rs_percentile for c in constituents]
        return max(0.0, min(sum(values) / len(values), 100.0))

    def calculate_momentum(self, constituents: List[SectorConstituent]) -> float:
        """Mean constituent return, linearly normalized to 0-100 using
        `config.momentum_floor_pct` / `config.momentum_cap_pct`.

        Args:
            constituents: Non-empty list of stocks belonging to one sector.

        Returns:
            0.0 if `constituents` is empty.
        """
        if not constituents:
            return 0.0
        avg_return = sum(c.momentum_pct for c in constituents) / len(constituents)

        floor, cap = self.config.momentum_floor_pct, self.config.momentum_cap_pct
        span = cap - floor
        if span <= 0:
            return 50.0
        normalized = ((avg_return - floor) / span) * 100.0
        return max(0.0, min(normalized, 100.0))

    def calculate_breadth(self, constituents: List[SectorConstituent]) -> float:
        """% of the sector's constituents trading above their key moving
        average, 0-100.

        Args:
            constituents: Non-empty list of stocks belonging to one sector.

        Returns:
            0.0 if `constituents` is empty.
        """
        if not constituents:
            return 0.0
        above = sum(1 for c in constituents if c.above_key_ma)
        return (above / len(constituents)) * 100.0

    def calculate_volume(self, constituents: List[SectorConstituent]) -> float:
        """Mean volume ratio of the sector's constituents, linearly
        normalized to 0-100 using `config.volume_ratio_floor` /
        `config.volume_ratio_cap`. Constituents with no volume data are
        excluded from the mean; if none have volume data, returns a
        neutral 50.0.

        Args:
            constituents: Non-empty list of stocks belonging to one sector.
        """
        ratios = [c.volume_ratio for c in constituents if c.volume_ratio is not None]
        if not ratios:
            return 50.0
        avg_ratio = sum(ratios) / len(ratios)

        floor, cap = self.config.volume_ratio_floor, self.config.volume_ratio_cap
        span = cap - floor
        if span <= 0:
            return 50.0
        normalized = ((avg_ratio - floor) / span) * 100.0
        return max(0.0, min(normalized, 100.0))

    # -- composite calculator ------------------------------------------------ #

    def _composite_score(
        self,
        relative_strength: float,
        momentum: float,
        breadth: float,
        volume: float,
    ) -> float:
        """Weighted blend of the four pillar scores, normalized by the
        configured weight total so an off-100 weights_sum still yields a
        valid 0-100 composite."""
        weights_sum = self.config.weights_sum
        if weights_sum <= 0:
            return 0.0
        raw = (
            relative_strength * self.config.weight_relative_strength
            + momentum * self.config.weight_momentum
            + breadth * self.config.weight_breadth
            + volume * self.config.weight_volume
        )
        return max(0.0, min(raw / weights_sum, 100.0))

    @staticmethod
    def _percentile_rank(scores: List[float], value: float) -> float:
        """% of `scores` that `value` is >= to (0-100). With a single
        sector in the run, returns 100.0."""
        if len(scores) <= 1:
            return 100.0
        at_or_below = sum(1 for s in scores if s <= value)
        return (at_or_below / len(scores)) * 100.0

    # -- public API ---------------------------------------------------------- #

    def score_sector(self, sector: str, constituents: List[SectorConstituent]) -> SectorScore:
        """Scores a single sector from its constituent rows, without
        percentile ranking against other sectors (percentile is set to
        0.0 — use `rank_sectors` for a cross-sector percentile).

        Args:
            sector: Sector name.
            constituents: Stocks belonging to this sector.

        Returns:
            SectorScore for this sector alone.
        """
        relative_strength = self.calculate_relative_strength(constituents)
        momentum = self.calculate_momentum(constituents)
        breadth = self.calculate_breadth(constituents)
        volume = self.calculate_volume(constituents)
        score = self._composite_score(relative_strength, momentum, breadth, volume)
        logger.debug(
            "score_sector: sector=%s n=%d rs=%.2f momentum=%.2f breadth=%.2f volume=%.2f score=%.2f",
            sector, len(constituents), relative_strength, momentum, breadth, volume, score,
        )

        return SectorScore(
            sector=sector,
            score=score,
            percentile=0.0,
            momentum=momentum,
            breadth=breadth,
            relative_strength=relative_strength,
            volume=volume,
            constituent_count=len(constituents),
        )

    def rank_sectors(self, constituents: List[SectorConstituent]) -> List[SectorScore]:
        """Groups constituents by sector, scores each sector, and returns
        them ranked from strongest to weakest with cross-sector
        `percentile` populated.

        Sectors with fewer than `config.min_constituents` rows are
        excluded from the output entirely (too little data to trust).

        Args:
            constituents: Flat list of SectorConstituent rows spanning any
                number of sectors.

        Returns:
            List[SectorScore] sorted by `score` descending.
        """
        by_sector: Dict[str, List[SectorConstituent]] = {}
        for c in constituents:
            by_sector.setdefault(c.sector, []).append(c)

        eligible = {
            sector: rows
            for sector, rows in by_sector.items()
            if len(rows) >= self.config.min_constituents
        }
        excluded = set(by_sector) - set(eligible)
        if excluded:
            logger.warning(
                "rank_sectors: excluding %d sector(s) below min_constituents=%d: %s",
                len(excluded), self.config.min_constituents, sorted(excluded),
            )

        raw_scores = {
            sector: self.score_sector(sector, rows) for sector, rows in eligible.items()
        }
        all_scores = [s.score for s in raw_scores.values()]

        ranked = [
            SectorScore(
                sector=s.sector,
                score=s.score,
                percentile=self._percentile_rank(all_scores, s.score),
                momentum=s.momentum,
                breadth=s.breadth,
                relative_strength=s.relative_strength,
                volume=s.volume,
                constituent_count=s.constituent_count,
            )
            for s in raw_scores.values()
        ]

        result = sorted(ranked, key=lambda s: s.score, reverse=True)
        logger.info("rank_sectors: ranked %d eligible sector(s)", len(result))
        return result

    def rank_sectors_from_dataframe(
        self,
        df: pd.DataFrame,
        sector_col: str = "Sector",
        symbol_col: str = "Symbol",
        rs_col: str = "RS_Pctl",
        momentum_col: str = "Momentum_Pct",
        above_ma_col: str = "Above_Key_MA",
        volume_ratio_col: Optional[str] = "Volume_Ratio",
    ) -> List[SectorScore]:
        """Convenience wrapper: builds SectorConstituent rows from a flat
        DataFrame (e.g. a discovery-style stock universe table) and calls
        `rank_sectors`. Purely a translation layer — no scoring logic here.

        Args:
            df: DataFrame with one row per stock.
            sector_col, symbol_col, rs_col, momentum_col, above_ma_col:
                Column names to read each SectorConstituent field from.
            volume_ratio_col: Column name for volume ratio, or None if the
                DataFrame doesn't carry volume data.

        Returns:
            List[SectorScore], same contract as `rank_sectors`.
        """
        if df is None or df.empty:
            return []

        constituents: List[SectorConstituent] = []
        for _, row in df.iterrows():
            volume_ratio = None
            if volume_ratio_col is not None and volume_ratio_col in df.columns:
                val = row.get(volume_ratio_col)
                volume_ratio = None if pd.isna(val) else float(val)

            constituents.append(
                SectorConstituent(
                    symbol=str(row.get(symbol_col)),
                    sector=str(row.get(sector_col)),
                    rs_percentile=float(row.get(rs_col, 0.0) or 0.0),
                    momentum_pct=float(row.get(momentum_col, 0.0) or 0.0),
                    above_key_ma=bool(row.get(above_ma_col, False)),
                    volume_ratio=volume_ratio,
                )
            )

        return self.rank_sectors(constituents)


# --------------------------------------------------------------------------- #
# Shared report helper (used by momentum_scanner.py and emfb.py)
# --------------------------------------------------------------------------- #

def print_rs_only_rotation_report(df: pd.DataFrame) -> None:
    """Informational-only sector rotation table, printed after a momentum/
    EMFB ranking table. Added 2026-08-21 after a week of the same
    "is this a sector move" question (defence, metals rotation) having to
    be answered by hand each time with an ad-hoc groupby - this automates
    that specific check using the engine above, which existed unused since
    before this file had any caller.

    Deliberately RS-only: SectorRotationEngine scores four pillars (RS,
    Momentum, Breadth, Volume), but momentum/EMFB's output DataFrame only
    carries a clean RS percentile (RS_vs_Nifty) - Momentum_Pct,
    Above_Key_MA and Volume_Ratio would each need new fields/API calls
    neither scan currently makes. Weighting RS at 100% and the other three
    at 0% reuses the engine's ranking/percentile machinery honestly rather
    than faking pillars with fabricated inputs; extending to the full
    4-pillar version is a separate follow-up, not silently pretended here.

    Never raises: callers wrap this in their own try/except too, but it
    also guards its own inputs so a caller that forgets to wrap it still
    degrades to "print nothing" rather than crashing the report.

    Args:
        df: The full ranked momentum/EMFB DataFrame (not just the printed
            top-N slice) - must have 'Sector', 'Symbol', 'RS_vs_Nifty'
            columns, or this silently does nothing.
    """
    if df is None or df.empty or 'Sector' not in df.columns or 'RS_vs_Nifty' not in df.columns:
        return

    rs_only_config = SectorRotationConfig(
        weight_relative_strength=100.0,
        weight_momentum=0.0,
        weight_breadth=0.0,
        weight_volume=0.0,
        min_constituents=3,
    )
    engine = SectorRotationEngine(rs_only_config)
    scores = engine.rank_sectors_from_dataframe(
        df, sector_col='Sector', symbol_col='Symbol', rs_col='RS_vs_Nifty',
        momentum_col='EMFB_Score',  # unused (weight=0); a real column is still required to exist
        above_ma_col='EMFB_Score',  # unused (weight=0); same reason
        volume_ratio_col=None,
    )
    if not scores:
        return

    print("\n" + "-" * 120)
    print("SECTOR ROTATION (RS-only composite - which sectors are outperforming as a group, not just one stock)")
    print("-" * 120)
    rows = [
        {
            'Sector': s.sector,
            'Avg_RS_Percentile': round(s.relative_strength, 1),
            'Sector_Percentile': round(s.percentile, 1),
            'Constituent_Count': s.constituent_count,
        }
        for s in scores
    ]
    print(pd.DataFrame(rows).to_string(index=False))
    print("-" * 120)

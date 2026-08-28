"""
Institutional Flow Engine (IAS Plug-in Module)

Vectorized measurement of institutional footprints — relative volume,
delivery percentage, and closing-range/CLV price-action dynamics — across
a universe of stocks, using Pandas/NumPy whole-DataFrame operations (no
per-ticker Python loops in the calculation path).

Design notes
------------
- Completely independent: no imports from config.py, data_broker.py,
  scanner_engine.py or any other existing module, and nothing in the
  existing codebase imports this one. Adding it carries zero regression
  risk to Version 1.0.
- All lookback windows, thresholds and weights live in
  `InstitutionalConfig`, a dataclass passed into the engine — nothing is
  hardcoded inline.
- No global/module-level mutable state. All state lives on an instance of
  InstitutionalFlowEngine (its `data` and `config`), passed explicitly by
  the caller.
- Calculation methods (`calculate_rvol`, `calculate_delivery_percent`,
  `calculate_closing_range`, `calculate_clv`, `calculate_accumulation_score`,
  `calculate_institutional_score`) operate on whole per-field DataFrames
  (DatetimeIndex x tickers) via vectorized Pandas ops — they never iterate
  over rows or the ticker list. The only per-ticker loops in the module
  are (a) reshaping the input dict of per-ticker OHLCV frames into wide
  per-field frames in `__init__`, and (b) packaging each column's latest
  values into its own InstitutionalFlowResult in `generate_metrics()` —
  both are unavoidable I/O-shape transforms, not part of the math itself.
- Logs at DEBUG for input shape/normalization and INFO for the final
  metrics-generation summary, via a module-level `logger` (standard
  `logging` module).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Output dataclass
# --------------------------------------------------------------------------- #

@dataclass
class InstitutionalFlowResult:
    """Latest institutional-footprint reading for a single ticker.

    Attributes:
        ticker: Stock ticker.
        rvol: Volume_today / SMA(Volume, rvol_window). NaN if there isn't
            yet a full `rvol_window` of volume history.
        delivery_percent: (DeliveryVolume / Volume) * 100, clipped to
            [0, 100].
        closing_range: ((Close - Low) / (High - Low)) * 100, clipped to
            [0, 100]; 50.0 on High == Low sessions.
        clv: Close Location Value, ((Close-Low)-(High-Close))/(High-Low),
            range [-1, 1]; 0.0 on High == Low sessions.
        accumulation_score: Rolling-sum footprint over
            `accumulation_window`, expressed as a 0-100 percentile rank
            against the rest of the universe on the latest day.
        institutional_score: 0-100 composite of normalized RVOL, delivery
            percentage and closing range, per `config` weights.
    """
    ticker: str
    rvol: float
    delivery_percent: float
    closing_range: float
    clv: float
    accumulation_score: float
    institutional_score: float


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class InstitutionalConfig:
    """All tunable windows, weights and thresholds for
    InstitutionalFlowEngine.

    Attributes:
        rvol_window: Trailing-session window for the volume SMA that RVOL
            is measured against.
        accumulation_window: Trailing-session window the accumulation
            footprint is summed over.
        weight_rvol: Weight of normalized RVOL in `institutional_score`.
        weight_delivery: Weight of delivery percentage in
            `institutional_score`.
        weight_closing_range: Weight of closing range in
            `institutional_score`.
        min_delivery_threshold: Minimum delivery percentage a session must
            clear for that session's volume*CLV footprint to count toward
            `accumulation_score`. This keeps the accumulation footprint
            focused on sessions with genuine delivery-based (as opposed to
            purely intraday/speculative) participation — a day with a
            strong CLV but low delivery isn't institutional accumulation,
            it's noise, so it's zeroed out of the rolling sum rather than
            being allowed to inflate it.
        rvol_cap: Upper cap applied to RVOL before normalizing it to 0-100
            for `institutional_score`.
    """
    rvol_window: int = 50
    accumulation_window: int = 20
    weight_rvol: float = 0.30
    weight_delivery: float = 0.40
    weight_closing_range: float = 0.30
    min_delivery_threshold: float = 40.0
    rvol_cap: float = 3.0


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class InstitutionalFlowEngine:
    """Vectorized calculator that turns per-ticker OHLCV+DeliveryVolume
    data into a per-ticker dict of InstitutionalFlowResult.

    Example:
        engine = InstitutionalFlowEngine(data, InstitutionalConfig())
        metrics = engine.generate_metrics()
        metrics['HAL.NS'].institutional_score
    """

    REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume", "DeliveryVolume")

    def __init__(
        self,
        data: Union[Dict[str, pd.DataFrame], pd.DataFrame],
        config: InstitutionalConfig = None,
    ):
        """
        Args:
            data: Either:
                - Dict[str, pd.DataFrame]: ticker -> OHLCV+DeliveryVolume
                  DataFrame (DatetimeIndex, sorted ascending), or
                - pd.DataFrame with MultiIndex columns (ticker, field),
                  field in REQUIRED_COLUMNS.
            config: Windows/weights/thresholds to use. Defaults to
                InstitutionalConfig() if not supplied.
        """
        self.config = config or InstitutionalConfig()
        (
            self.high,
            self.low,
            self.close,
            self.volume,
            self.delivery_volume,
        ) = self._normalize_input(data)

    # -- input normalization (shape transform, not math) ------------------- #

    def _normalize_input(self, data: Union[Dict[str, pd.DataFrame], pd.DataFrame]):
        """Reshapes either accepted input form into five wide per-field
        DataFrames (DatetimeIndex x tickers): High, Low, Close, Volume,
        DeliveryVolume. This is a structural transform, not a calculation.
        """
        if isinstance(data, dict):
            tickers = list(data.keys())
            high = pd.concat({t: data[t]["High"] for t in tickers}, axis=1)
            low = pd.concat({t: data[t]["Low"] for t in tickers}, axis=1)
            close = pd.concat({t: data[t]["Close"] for t in tickers}, axis=1)
            volume = pd.concat({t: data[t]["Volume"] for t in tickers}, axis=1)
            delivery = pd.concat({t: data[t]["DeliveryVolume"] for t in tickers}, axis=1)
        elif isinstance(data, pd.DataFrame):
            high = data.xs("High", axis=1, level=1)
            low = data.xs("Low", axis=1, level=1)
            close = data.xs("Close", axis=1, level=1)
            volume = data.xs("Volume", axis=1, level=1)
            delivery = data.xs("DeliveryVolume", axis=1, level=1)
        else:
            raise TypeError(
                "data must be a Dict[str, pd.DataFrame] or a pd.DataFrame with "
                "MultiIndex columns (ticker, field)"
            )

        return (
            high.sort_index(),
            low.sort_index(),
            close.sort_index(),
            volume.sort_index(),
            delivery.sort_index(),
        )

    # -- vectorized calculators ------------------------------------------- #

    def calculate_rvol(self) -> pd.DataFrame:
        """Relative Volume at every date for every ticker:
        Volume_today / SMA(Volume, rvol_window).

        Returns:
            DataFrame aligned to `self.volume`, NaN before a full
            `rvol_window` of history is available or where the SMA is
            zero (guards divide-by-zero).
        """
        sma_volume = self.volume.rolling(window=self.config.rvol_window).mean()
        safe_sma = sma_volume.where(sma_volume > 0, other=np.nan)
        return self.volume / safe_sma

    def calculate_delivery_percent(self) -> pd.DataFrame:
        """Delivery percentage at every date for every ticker:
        (DeliveryVolume / Volume) * 100, clipped to [0, 100].

        Returns:
            DataFrame aligned to `self.volume`, NaN where Volume is zero
            or missing (guards divide-by-zero).
        """
        safe_volume = self.volume.where(self.volume > 0, other=np.nan)
        pct = (self.delivery_volume / safe_volume) * 100.0
        return pct.clip(lower=0.0, upper=100.0)

    def calculate_closing_range(self) -> pd.DataFrame:
        """Closing Range at every date for every ticker:
        ((Close - Low) / (High - Low)) * 100, clipped to [0, 100].
        50.0 on sessions where High == Low.

        Returns:
            DataFrame aligned to `self.close`.
        """
        span = self.high - self.low
        safe_span = span.where(span != 0, other=np.nan)
        value = ((self.close - self.low) / safe_span) * 100.0
        value = value.clip(lower=0.0, upper=100.0)
        return value.where(span != 0, other=50.0)

    def calculate_clv(self) -> pd.DataFrame:
        """Close Location Value at every date for every ticker:
        ((Close-Low)-(High-Close)) / (High-Low), range [-1, 1].
        0.0 on sessions where High == Low.

        Returns:
            DataFrame aligned to `self.close`.
        """
        span = self.high - self.low
        safe_span = span.where(span != 0, other=np.nan)
        value = ((self.close - self.low) - (self.high - self.close)) / safe_span
        return value.where(span != 0, other=0.0)

    def calculate_accumulation_score(
        self, delivery_percent: pd.DataFrame, clv: pd.DataFrame
    ) -> pd.DataFrame:
        """Rolling institutional-footprint score at every date for every
        ticker.

        Daily footprint = Volume * CLV * (DeliveryPercent / 100), with
        sessions below `config.min_delivery_threshold` zeroed out (see
        `InstitutionalConfig.min_delivery_threshold`). Accumulation Score
        is the rolling sum of that footprint over `accumulation_window`,
        expressed as a 0-100 percentile rank against the rest of the
        universe on each date.

        Args:
            delivery_percent: As returned by `calculate_delivery_percent`.
            clv: As returned by `calculate_clv`.

        Returns:
            DataFrame aligned to `self.volume`.
        """
        cfg = self.config
        qualifies = delivery_percent >= cfg.min_delivery_threshold
        daily_footprint = self.volume * clv * (delivery_percent / 100.0)
        daily_footprint = daily_footprint.where(qualifies, other=0.0)

        rolling_footprint = daily_footprint.rolling(window=cfg.accumulation_window).sum()
        return rolling_footprint.rank(axis=1, pct=True) * 100.0

    def calculate_institutional_score(
        self, rvol: pd.DataFrame, delivery_percent: pd.DataFrame, closing_range: pd.DataFrame
    ) -> pd.DataFrame:
        """0-100 composite of the current day's institutional footprint:
        (Normalized_RVOL * weight_rvol) + (DeliveryPercent * weight_delivery)
        + (ClosingRange * weight_closing_range), where Normalized_RVOL is
        RVOL capped at `config.rvol_cap` and scaled to 0-100.

        Args:
            rvol: As returned by `calculate_rvol`.
            delivery_percent: As returned by `calculate_delivery_percent`.
            closing_range: As returned by `calculate_closing_range`.

        Returns:
            DataFrame aligned to `self.volume`.
        """
        cfg = self.config
        normalized_rvol = (rvol.clip(upper=cfg.rvol_cap) / cfg.rvol_cap) * 100.0
        return (
            normalized_rvol * cfg.weight_rvol
            + delivery_percent * cfg.weight_delivery
            + closing_range * cfg.weight_closing_range
        )

    # -- public API ---------------------------------------------------------- #

    def generate_metrics(self) -> Dict[str, InstitutionalFlowResult]:
        """Runs the full vectorized pipeline and packages the latest
        state for every ticker.

        Returns:
            Dict mapping ticker -> InstitutionalFlowResult for every
            ticker present in the input. Tickers with insufficient
            history for a given metric get NaN for that metric rather
            than being dropped or raising.
        """
        if self.close.empty:
            logger.warning("InstitutionalFlowEngine.generate_metrics: no data after normalization")
            return {}

        logger.debug(
            "InstitutionalFlowEngine.generate_metrics: %d tickers, %d sessions",
            self.close.shape[1], self.close.shape[0],
        )
        rvol = self.calculate_rvol()
        delivery_percent = self.calculate_delivery_percent()
        closing_range = self.calculate_closing_range()
        clv = self.calculate_clv()
        accumulation_score = self.calculate_accumulation_score(delivery_percent, clv)
        institutional_score = self.calculate_institutional_score(rvol, delivery_percent, closing_range)

        latest_date = self.close.index[-1]
        latest_rvol = rvol.loc[latest_date]
        latest_delivery = delivery_percent.loc[latest_date]
        latest_closing_range = closing_range.loc[latest_date]
        latest_clv = clv.loc[latest_date]
        latest_accumulation = accumulation_score.loc[latest_date]
        latest_institutional = institutional_score.loc[latest_date]

        results: Dict[str, InstitutionalFlowResult] = {}
        for ticker in self.close.columns:
            results[ticker] = InstitutionalFlowResult(
                ticker=ticker,
                rvol=self._safe_float(latest_rvol[ticker]),
                delivery_percent=self._safe_float(latest_delivery[ticker]),
                closing_range=self._safe_float(latest_closing_range[ticker]),
                clv=self._safe_float(latest_clv[ticker]),
                accumulation_score=self._safe_float(latest_accumulation[ticker]),
                institutional_score=self._safe_float(latest_institutional[ticker]),
            )
        logger.info("InstitutionalFlowEngine.generate_metrics: generated metrics for %d ticker(s)", len(results))
        return results

    @staticmethod
    def _safe_float(value) -> float:
        """Converts a scalar to float, preserving NaN instead of raising."""
        return float(value) if pd.notna(value) else np.nan


# --------------------------------------------------------------------------- #
# Shared informational-score helper
# --------------------------------------------------------------------------- #

def compute_institutional_scores(daily_data_by_symbol: Dict[str, pd.DataFrame]) -> "pd.Series":
    """Standalone, NaN-safe helper: computes Institutional_Score (0-100) per
    symbol from real NSE bhavcopy delivery data (RVOL, delivery %, closing
    range - see InstitutionalFlowEngine), for the most recently published
    bhavcopy trading day.

    Extracted from discovery.py's `_augment_with_institutional_score`
    (2026-08-09, commit 4d1cc15) so momentum/EMFB scan output can surface
    the same informational field discovery.py already does, without
    duplicating the fetch-and-score glue. discovery.py's own function is
    left untouched (proven, in production) - this is purely additive.

    Args:
        daily_data_by_symbol: symbol -> daily OHLCV DataFrame (must have a
            'Timestamp' column), already resolved by the caller.

    Returns:
        Series indexed by symbol -> institutional_score. Symbols with no
        resolvable score are simply absent - callers should reindex with a
        NaN fill for any symbol they need represented. Empty Series (never
        raises) if NSE's bhavcopy is unreachable, not yet published for a
        holiday, or any other failure occurs - this must never break the
        caller's existing output.

    Same informational-only convention as discovery.py: intentionally NOT
    intended to be wired into any Rank_Score/sort/filter. One month of
    validation (see SESSION_NOTES.md) showed ~0 individual-stock rank
    correlation with 3-5 day forward returns, but a real 65-70% top-vs-
    bottom-quintile beat rate - a genuine but not yet individually-reliable
    signal. Revisit ranking use only after 4-6+ weeks of live history.

    T-1 lagged: NSE's bhavcopy for "today" isn't published until after
    close, so this always reflects the most recently published trading
    day, never the live session.
    """
    try:
        from nse_delivery_feed import NSEDeliveryFeed

        symbols = list(daily_data_by_symbol.keys())
        feed = NSEDeliveryFeed()
        try:
            history = feed.fetch_delivery_history(symbols, trading_days=1)
        finally:
            feed.close()

        if history.empty:
            logger.warning("Institutional_Score skipped: no recent NSE bhavcopy available.")
            return pd.Series(dtype=float)
        target_date = history.index[0]

        engine_data = {}
        for symbol, daily_df in daily_data_by_symbol.items():
            if daily_df is None or daily_df.empty:
                continue
            df = daily_df.copy()
            df['_d'] = pd.to_datetime(df['Timestamp']).dt.tz_localize(None).dt.normalize()
            df = df.drop_duplicates(subset='_d').set_index('_d').sort_index()
            df['DeliveryVolume'] = 0.0
            if target_date in df.index and symbol in history.columns:
                df.loc[target_date, 'DeliveryVolume'] = history.loc[target_date, symbol]
            engine_data[symbol] = df

        if not engine_data:
            logger.warning("Institutional_Score skipped: no usable daily history for any symbol.")
            return pd.Series(dtype=float)

        engine = InstitutionalFlowEngine(engine_data)
        rvol = engine.calculate_rvol()
        delivery_pct = engine.calculate_delivery_percent()
        closing_range = engine.calculate_closing_range()
        institutional_score = engine.calculate_institutional_score(rvol, delivery_pct, closing_range)

        if target_date not in institutional_score.index:
            logger.warning(f"Institutional_Score skipped: {target_date.date()} not present in computed output.")
            return pd.Series(dtype=float)

        return institutional_score.loc[target_date]
    except Exception as e:
        logger.warning(f"Institutional_Score computation skipped due to an error: {e}", exc_info=True)
        return pd.Series(dtype=float)

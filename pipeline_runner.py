"""
IAS Pipeline Runner (IAS Orchestrator)

Wires the ten IAS plug-in modules together into one daily, end-to-end run:

    Update Data (DataBroker, read-only)
      -> Market Context
      -> Relative Strength
      -> Sector Rotation (reuses Relative Strength's per-ticker RS)
      -> Institutional Flow
      -> Breakout Engine
      -> Integration Adapter
      -> Final Ranker
      -> Analytics Repository (persist)
      -> Dashboard (render)
      -> Validation Report (optional, diagnostics only — see PipelineConfig.enable_validation_report)

Usage
-----
    python pipeline_runner.py

Design notes
------------
- This is the one IAS file that legitimately reads from Version 1.0
  infrastructure: `data_broker.DataBroker` (for price history) and
  `config.Universe.TARGET_UNIVERSE` / `config.Universe.SECTOR_MAP` (for the
  scan universe and sector mapping). It only *reads* — it never calls a
  method that mutates DataBroker/config state, and V1.0's own entry points
  (main.py, orchestrator.py, etc.) are untouched and keep working exactly
  as before, whether or not this file is ever run.
- No modifications to any existing file, IAS or V1.0.
- The five scoring engines and Integration Adapter remain fully decoupled
  from this file's concerns — this module's only job is data plumbing
  (fetch -> shape -> call each engine -> pass results on) and persistence/
  presentation invocation. Any calculation here is explicitly *data
  aggregation from already-fetched raw prices* (advance/decline counts,
  breadth %, per-ticker momentum for sector scoring), not new scoring
  logic — see `_compute_market_aggregates` and `_build_sector_constituents`
  for exactly what that means and why it lives at this layer rather than
  inside an engine.
- Explicit, documented failure policy (see audit finding H-01): if the
  index data (NIFTY/BANKNIFTY/India VIX) can't be fetched, the run aborts
  entirely — Market Context cannot function without it. If an individual
  ticker's price history is missing or too short, that ticker alone is
  skipped (logged) and the rest of the run proceeds — one bad symbol does
  not take down the whole day's scan.
- Known limitation, stated rather than hidden: DataBroker has no delivery-
  volume data source wired up yet, so `institutional_flow`'s DeliveryVolume
  input is populated with 0.0 (not NaN — see `_augment_with_delivery_volume`
  for why) until a real delivery-% feed is connected. Delivery-dependent
  metrics are honestly conservative, not fabricated, in the meantime.
- All tunable knobs (universe override, history window, worker count, DB
  path, dashboard on/off, top-N sizes) live in `PipelineConfig` — nothing
  hardcoded inline.
- Every intermediate result (MarketContext, per-engine outputs,
  TickerInputs, FinalRankResult) is bundled into a
  `validation_report.PipelineRunArtifacts` and stored on
  `self.last_run` after each `run()` call — additively; `run()`'s return
  value and behavior are unchanged. This is what lets
  `validation_report.py` explain a run's results without recomputing any
  of them. Printing the report itself is opt-in via
  `PipelineConfig.enable_validation_report` (default False) so the
  default `python pipeline_runner.py` output is unchanged.
- Logs at INFO for each pipeline stage's start/finish and duration, WARNING
  for skipped tickers/fallbacks, and a final run summary, via a
  module-level `logger` (standard `logging` module). This is also the
  file's `if __name__ == "__main__"` block that finally calls
  `logging.basicConfig`, since every other IAS module deliberately leaves
  that decision to whatever process imports it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from analytics_repository import AnalyticsRepository, ScanRecord
from breakout_engine import BreakoutAnalysis, BreakoutEngine
from dashboard import TerminalDashboard
from final_ranker import AdaptiveRanker, RankerConfig
from institutional_flow import InstitutionalFlowEngine, InstitutionalFlowResult
from integration_adapter import IntegrationAdapter
from nse_delivery_feed import NSEDeliveryFeed
from market_context import (
    AdvanceDeclineData,
    BreadthData,
    MarketContext,
    MarketContextEngine,
    MarketContextInputs,
)
from relative_strength import RelativeStrengthEngine, RelativeStrengthResult
from sector_rotation import SectorConstituent, SectorRotationEngine, SectorScore
from weekly_review import SystemReviewEngine
from validation_report import (
    PipelineRunArtifacts,
    ValidationReportConfig,
    ValidationReportEngine,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PipelineConfig:
    """All tunable knobs for a PipelineRunner run.

    Attributes:
        universe: Tickers to scan. Defaults to
            `config.Universe.TARGET_UNIVERSE` (V1.0's own universe, read
            only) when None.
        history_days: Trading-calendar days of daily OHLCV history to
            request per symbol. 460 calendar days yields ~300+ trading
            sessions after weekends/NSE holidays, enough buffer above the
            272 sessions relative_strength's RS Acceleration needs (252
            for the 12M ROC composite + 20 for the acceleration lookback
            itself) - 400 calendar days (~271-272 sessions) sat right at
            that boundary and produced all-NaN acceleration.
        min_history_days: Minimum sessions of history a ticker must have
            to be scored at all this run. Tickers below this are skipped
            (logged), not passed through to produce all-NaN engine output.
        min_valid_rs_fraction: Minimum fraction of fetched tickers that
            must produce a non-NaN Relative Strength composite score.
            Below this, the run aborts (RuntimeError) instead of persisting
            a degenerate day where most/all alpha_scores silently default
            to 0.0 - each fetched ticker already passed min_history_days,
            so a mass NaN collapse here means the day's price data itself
            was bad (broker fetch returning empty/flat/duplicate candles),
            not a real quiet market (see incident 2026-07-29: 210/214
            tickers NaN'd out and analytics.db recorded an all-zero day
            that looked like "no opportunities" instead of a failed run).
        max_workers: Parallel fetch workers. Defaults to
            `config.AppConfig.SAFE_API_WORKERS` (V1.0's own rate-limit-safe
            worker count) when None.
        db_path: SQLite path for AnalyticsRepository.
        ranker_config_path: Path to ranker_config.yaml.
        enable_dashboard: Whether to render the terminal dashboard at the
            end of the run.
        top_n_dashboard: Rows shown per dashboard table.
        top_n_alpha_picks: Rows shown in the Final Alpha Picks panel.
        enable_validation_report: Whether to build and print the
            diagnostics report (validation_report.py) at the end of the
            run. Off by default so the standard `python pipeline_runner.py`
            output is unchanged; `self.last_run` is always populated
            regardless, so a caller can generate the report afterward
            without this flag.
        validation_report_config: Thresholds for the diagnostics report.
            Defaults to ValidationReportConfig() when None.
        delivery_lookback_days: Trading days of NSE bhavcopy delivery data
            to fetch for institutional_flow.py's DeliveryVolume input.
            institutional_flow.py's own accumulation_window (20 sessions)
            is the longest lookback anything currently does with delivery
            data - rvol_window (50) is volume-only, not delivery - so this
            deliberately doesn't need to match history_days (460); a
            smaller, independent window keeps the daily bhavcopy fetch
            (one HTTP call per day, real network I/O against NSE) fast.
    """
    universe: Optional[List[str]] = None
    history_days: int = 460
    min_history_days: int = 280
    min_valid_rs_fraction: float = 0.5
    max_workers: Optional[int] = None
    db_path: str = "analytics.db"
    ranker_config_path: str = "ranker_config.yaml"
    enable_dashboard: bool = True
    top_n_dashboard: int = 10
    top_n_alpha_picks: int = 10
    enable_validation_report: bool = False
    validation_report_config: Optional[ValidationReportConfig] = None
    delivery_lookback_days: int = 30


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

class PipelineRunner:
    """Runs the complete IAS workflow end-to-end for one trading day.

    Example:
        PipelineRunner().run()
    """

    def __init__(
        self,
        broker=None,
        pipeline_config: Optional[PipelineConfig] = None,
        adapter: Optional[IntegrationAdapter] = None,
    ):
        """
        Args:
            broker: An object exposing `.fetch_ohlcv(symbol, interval,
                days_back, caller=...) -> pd.DataFrame` with columns
                Timestamp/Open/High/Low/Close/Volume, matching
                `data_broker.DataBroker`'s interface. If None, a real
                `DataBroker` is constructed lazily on first use (so simply
                importing/instantiating PipelineRunner never requires live
                broker credentials — only actually calling `run()` does).
                Passing a test double here is how this module is
                unit-tested without a live session.
            pipeline_config: Knobs to use. Defaults to PipelineConfig().
            adapter: IntegrationAdapter to use. Defaults to
                IntegrationAdapter() (which itself defaults its
                AdapterConfig).
        """
        self.config = pipeline_config or PipelineConfig()
        self._broker = broker
        self.adapter = adapter or IntegrationAdapter()
        self.ranker_config = RankerConfig.load(self.config.ranker_config_path)
        self.repository = AnalyticsRepository(self.config.db_path)
        self.review_engine = SystemReviewEngine(self.repository, self.ranker_config)
        self.validation_engine = ValidationReportEngine(self.config.validation_report_config)
        self.last_run: Optional[PipelineRunArtifacts] = None
        # Lazy, same pattern as self._broker above - constructing
        # PipelineRunner never makes a network call by itself.
        self._delivery_feed: Optional[NSEDeliveryFeed] = None

    # -- data access ----------------------------------------------------------- #

    def _get_broker(self):
        """Lazily constructs the real DataBroker on first use, unless a
        broker (or test double) was already supplied to __init__."""
        if self._broker is None:
            from data_broker import DataBroker  # local import: avoid requiring live credentials just to import this module
            logger.info("_get_broker: constructing live DataBroker session")
            self._broker = DataBroker()
        return self._broker

    @staticmethod
    def _to_indexed(df: pd.DataFrame) -> pd.DataFrame:
        """Converts a DataBroker-shaped DataFrame (Timestamp column) into
        the DatetimeIndex-sorted-ascending shape every IAS engine expects."""
        if df is None or df.empty:
            return pd.DataFrame()
        indexed = df.set_index("Timestamp").sort_index()
        return indexed

    def _fetch_index_data(self, broker) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Fetches NIFTY 50 / NIFTY BANK / India VIX history.

        Returns:
            (nifty_df, banknifty_df, vix_df), each DatetimeIndex-sorted
            with an Open/High/Low/Close/Volume shape.

        Raises:
            RuntimeError: If any of the three can't be fetched — Market
                Context cannot function without them, so the run aborts
                here rather than producing a misleading regime read.
        """
        symbols = {"Nifty 50": "nifty", "NIFTY BANK": "banknifty", "India VIX": "vix"}
        frames = {}
        for symbol, key in symbols.items():
            raw = broker.fetch_ohlcv(symbol, "ONE_DAY", self.config.history_days, caller="PipelineRunner")
            indexed = self._to_indexed(raw)
            if indexed.empty:
                raise RuntimeError(
                    f"_fetch_index_data: could not fetch '{symbol}' — aborting run; "
                    "Market Context requires all three index series."
                )
            frames[key] = indexed
        return frames["nifty"], frames["banknifty"], frames["vix"]

    def _fetch_universe_data(self, broker, tickers: List[str]) -> Tuple[Dict[str, pd.DataFrame], List[str]]:
        """Fetches OHLCV history for every ticker in parallel, skipping
        (with a warning) any ticker that fails to fetch or has fewer than
        `config.min_history_days` sessions.

        Args:
            broker: Broker-like object with `.fetch_ohlcv(...)`.
            tickers: Tickers to fetch.

        Returns:
            (Dict[ticker, DataFrame] for tickers with sufficient history,
            List[ticker] skipped for missing/insufficient history — kept
            so validation_report.py can report exactly which tickers were
            eliminated before scoring even started, and why).
        """
        max_workers = self.config.max_workers or config.AppConfig.SAFE_API_WORKERS
        results: Dict[str, pd.DataFrame] = {}
        skipped: List[str] = []

        def fetch_one(ticker: str) -> Tuple[str, pd.DataFrame]:
            raw = broker.fetch_ohlcv(ticker, "ONE_DAY", self.config.history_days, caller="PipelineRunner")
            return ticker, self._to_indexed(raw)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_one, t): t for t in tickers}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    _, df = future.result()
                except Exception as exc:
                    logger.warning("_fetch_universe_data: %s fetch raised %r; skipping", ticker, exc)
                    skipped.append(ticker)
                    continue
                if df.empty or len(df) < self.config.min_history_days:
                    logger.warning(
                        "_fetch_universe_data: %s has %d session(s), below min_history_days=%d; skipping",
                        ticker, len(df), self.config.min_history_days,
                    )
                    skipped.append(ticker)
                    continue
                results[ticker] = df

        logger.info("_fetch_universe_data: fetched %d/%d ticker(s) with sufficient history", len(results), len(tickers))
        return results, skipped

    # -- data aggregation (not scoring) ---------------------------------------- #

    def _compute_market_aggregates(
        self, price_data: Dict[str, pd.DataFrame], sector_map: Dict[str, str]
    ) -> Tuple[AdvanceDeclineData, BreadthData, Dict[str, float]]:
        """Aggregates already-fetched raw prices into the cross-sectional
        inputs market_context.py itself needs (advance/decline counts,
        % above 50-DMA, per-sector breadth). This is plain aggregation of
        data this module already fetched — not a scoring decision, so it
        stays out of market_context.py itself (which only knows about
        NIFTY/BANKNIFTY/VIX, not the full universe).

        Args:
            price_data: ticker -> OHLCV DataFrame, as returned by
                `_fetch_universe_data`.
            sector_map: ticker -> sector name.

        Returns:
            (AdvanceDeclineData, BreadthData, sector_breadth) ready for
            MarketContextInputs.
        """
        advances = declines = 0
        above_50dma = 0
        total = 0
        sector_above = {}
        sector_total = {}

        for ticker, df in price_data.items():
            if len(df) < 51:
                continue
            close = df["Close"]
            if close.iloc[-1] > close.iloc[-2]:
                advances += 1
            elif close.iloc[-1] < close.iloc[-2]:
                declines += 1

            sma50 = close.rolling(50).mean().iloc[-1]
            total += 1
            is_above = bool(close.iloc[-1] > sma50)
            if is_above:
                above_50dma += 1

            sector = sector_map.get(ticker, "OTHER")
            sector_total[sector] = sector_total.get(sector, 0) + 1
            if is_above:
                sector_above[sector] = sector_above.get(sector, 0) + 1

        breadth_pct = (above_50dma / total * 100.0) if total else 0.0
        sector_breadth = {
            sector: (sector_above.get(sector, 0) / count * 100.0)
            for sector, count in sector_total.items()
        }

        return (
            AdvanceDeclineData(advances=advances, declines=declines),
            BreadthData(pct_above_50dma=breadth_pct),
            sector_breadth,
        )

    def _build_sector_constituents(
        self,
        price_data: Dict[str, pd.DataFrame],
        sector_map: Dict[str, str],
        rs_results: Dict[str, RelativeStrengthResult],
    ) -> List[SectorConstituent]:
        """Builds sector_rotation's SectorConstituent rows, reusing
        relative_strength's already-computed rs_percentile/composite_score
        for each ticker rather than recomputing a second, inconsistent RS
        figure. above_key_ma and volume_ratio are simple reads off the raw
        price data this module already fetched.

        Args:
            price_data: ticker -> OHLCV DataFrame.
            sector_map: ticker -> sector name.
            rs_results: relative_strength output, as returned by
                RelativeStrengthEngine.calculate().

        Returns:
            List[SectorConstituent], one per ticker with both price data
            and a relative-strength result available.
        """
        constituents = []
        for ticker, df in price_data.items():
            rs = rs_results.get(ticker)
            if rs is None or rs.rs_rank is None:
                continue

            close = df["Close"]
            sma50 = close.rolling(50).mean().iloc[-1]
            above_key_ma = bool(close.iloc[-1] > sma50) if pd.notna(sma50) else False

            volume = df["Volume"]
            avg_volume_20 = volume.rolling(20).mean().iloc[-1]
            volume_ratio = float(volume.iloc[-1] / avg_volume_20) if avg_volume_20 and avg_volume_20 > 0 else None

            # composite_score is a weighted-ROC %, the same units
            # SectorRotationConfig's momentum_floor_pct/momentum_cap_pct
            # expect — rs_percentile (0-100) would not fit that scale.
            momentum_pct = rs.composite_score if rs.composite_score == rs.composite_score else 0.0

            constituents.append(
                SectorConstituent(
                    symbol=ticker,
                    sector=sector_map.get(ticker, "OTHER"),
                    rs_percentile=rs.rs_percentile,
                    momentum_pct=momentum_pct,
                    above_key_ma=above_key_ma,
                    volume_ratio=volume_ratio,
                )
            )
        return constituents

    def _augment_with_delivery_volume(self, price_data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """Adds a DeliveryVolume column to each ticker's OHLCV frame for
        institutional_flow.py's input contract.

        Backed by nse_delivery_feed.py (NSE's public daily bhavcopy, real
        DELIV_QTY per symbol - see that module's docstring) since
        2026-08-07. Falls back to the original 0.0-everywhere behavior if
        the feed is unreachable or returns nothing usable - a delivery-data
        outage must never take down the whole pipeline run, matching this
        file's own stated failure policy ("one bad symbol does not take
        down the whole day's scan"). 0.0 (not NaN) is still the fallback
        value for any (ticker, date) the feed doesn't cover: institutional_
        flow's delivery_percent would then read 0.0 (an honest, conservative
        "no delivery signal available"), not NaN, which would corrupt the
        RVOL/closing-range components of institutional_score too.

        Args:
            price_data: ticker -> OHLCV DataFrame (DatetimeIndex).

        Returns:
            New dict of ticker -> DataFrame with a DeliveryVolume column
            added (originals are not mutated).
        """
        if self._delivery_feed is None:
            self._delivery_feed = NSEDeliveryFeed()

        tickers = list(price_data.keys())
        try:
            history = self._delivery_feed.fetch_delivery_history(
                tickers, trading_days=self.config.delivery_lookback_days
            )
        except Exception as e:
            logger.warning(f"NSE delivery feed failed ({e}); falling back to 0.0 DeliveryVolume for this run.")
            history = pd.DataFrame(columns=tickers)

        augmented = {}
        for ticker, df in price_data.items():
            with_delivery = df.copy()
            if ticker in history.columns:
                with_delivery["DeliveryVolume"] = history[ticker].reindex(df.index).fillna(0.0)
            else:
                with_delivery["DeliveryVolume"] = 0.0
            augmented[ticker] = with_delivery
        return augmented

    # -- persistence & presentation -------------------------------------------- #

    def _persist_results(
        self,
        scan_date: str,
        market_context: MarketContext,
        sector_scores: Dict[str, SectorScore],
        sector_map: Dict[str, str],
        rs_results: Dict[str, RelativeStrengthResult],
        inst_results: Dict[str, InstitutionalFlowResult],
        breakout_results: Dict[str, BreakoutAnalysis],
        final_results: dict,
    ) -> None:
        """Builds one ScanRecord per scored ticker and batch-logs them via
        AnalyticsRepository. scanner_score currently mirrors alpha_score —
        V1.0's own scanner_engine execution score isn't wired in as a
        distinct input yet; documented here rather than silently assumed.
        """
        records = []
        for ticker, result in final_results.items():
            sector = sector_map.get(ticker, "OTHER")
            sector_score = sector_scores.get(sector)
            rs = rs_results.get(ticker)
            inst = inst_results.get(ticker)
            breakout = breakout_results.get(ticker)

            records.append(
                ScanRecord(
                    scan_date=scan_date,
                    ticker=ticker,
                    scanner_score=result.alpha_score,
                    sector_score=sector_score.score if sector_score else 0.0,
                    rs_score=(rs.composite_score if rs and rs.composite_score == rs.composite_score else 0.0),
                    inst_score=inst.institutional_score if inst else 0.0,
                    breakout_score=breakout.confidence if breakout else 0.0,
                    alpha_score=result.alpha_score,
                    market_regime=market_context.regime,
                    is_selected=result.position_size > 0,
                )
            )
        self.repository.log_daily_scans(records)

    def _build_master_state(
        self,
        market_context: MarketContext,
        sector_scores: List[SectorScore],
        rs_results: Dict[str, RelativeStrengthResult],
        inst_results: Dict[str, InstitutionalFlowResult],
        breakout_results: Dict[str, BreakoutAnalysis],
        final_results: dict,
        sector_map: Dict[str, str],
        tickers_scanned: int,
    ) -> dict:
        """Assembles the master_state dict dashboard.TerminalDashboard
        expects. This only selects/formats already-computed values (top-N
        slicing for display) — no scoring happens here."""
        cfg = self.config

        sector_rows = [
            {
                "rank": idx,
                "sector": s.sector,
                "percentile": s.percentile,
                "sector_multiplier": self.adapter.derive_sector_multiplier(s),
            }
            for idx, s in enumerate(sector_scores[: cfg.top_n_dashboard], start=1)
        ]

        rs_rows = sorted(
            (r for r in rs_results.values() if r.rs_rank is not None),
            key=lambda r: r.rs_rank,
        )[: cfg.top_n_dashboard]

        inst_rows = sorted(
            inst_results.values(), key=lambda r: r.institutional_score, reverse=True
        )[: cfg.top_n_dashboard]

        breakout_rows = [b for b in breakout_results.values() if b.trend_template_pass][: cfg.top_n_dashboard]

        ranked_final = sorted(final_results.values(), key=lambda r: r.alpha_score, reverse=True)
        alpha_picks = [
            {
                "ticker": r.ticker,
                "alpha_score": r.alpha_score,
                "probability": r.probability,
                "position_size": r.position_size,
                "sector": sector_map.get(r.ticker, "OTHER"),
                "detected_patterns": (
                    breakout_results[r.ticker].detected_patterns if r.ticker in breakout_results else []
                ),
            }
            for r in ranked_final[: cfg.top_n_alpha_picks]
            if r.position_size > 0
        ]

        stocks_passed = sum(1 for r in final_results.values() if r.position_size > 0)
        scanner_version = "IAS-2.0"
        weekly_report = self.review_engine.generate_weekly_report()

        return {
            "market_context": market_context,
            "sector_data": sector_rows,
            "relative_strength": rs_rows,
            "institutional_flow": inst_rows,
            "breakouts": breakout_rows,
            "alpha_picks": alpha_picks,
            "scanner_stats": {
                "stocks_scanned": tickers_scanned,
                "stocks_passed": stocks_passed,
                "market_regime": market_context.regime,
                "hit_rate": weekly_report.hit_rate * 100.0,
                "false_positives_last_week": weekly_report.false_positives,
                "missed_winners_last_week": weekly_report.missed_winners,
                "model_version": scanner_version,
            },
            "system_info": {
                "scanner_version": scanner_version,
                "position_size_recommendation": alpha_picks[0]["position_size"] if alpha_picks else None,
            },
        }

    # -- public API ------------------------------------------------------------ #

    def run(self) -> dict:
        """Executes the complete IAS pipeline for one trading day: fetch,
        score (five engines), translate (adapter), rank, persist, render.

        Returns:
            Dict mapping ticker -> FinalRankResult for the day's scored
            universe (only tickers with sufficient history are included).
        """
        run_start = time.perf_counter()
        scan_date = datetime.now(config.MARKET_TZ).strftime("%Y-%m-%d")
        logger.info("run: starting IAS pipeline for scan_date=%s", scan_date)

        broker = self._get_broker()
        tickers = self.config.universe or config.Universe.TARGET_UNIVERSE
        sector_map = config.Universe.SECTOR_MAP

        stage_start = time.perf_counter()
        nifty, banknifty, vix = self._fetch_index_data(broker)
        price_data, skipped_tickers = self._fetch_universe_data(broker, tickers)
        logger.info("run: data fetch complete in %.1fs", time.perf_counter() - stage_start)

        if not price_data:
            raise RuntimeError("run: no tickers had sufficient history; aborting.")

        stage_start = time.perf_counter()
        advance_decline, breadth, sector_breadth = self._compute_market_aggregates(price_data, sector_map)
        market_context = MarketContextEngine().analyze(
            MarketContextInputs(
                nifty=nifty,
                banknifty=banknifty,
                india_vix=vix,
                advance_decline=advance_decline,
                breadth=breadth,
                sector_breadth=sector_breadth,
            )
        )
        logger.info(
            "run: Market Context complete in %.1fs (regime=%s score=%.1f)",
            time.perf_counter() - stage_start, market_context.regime, market_context.score,
        )

        stage_start = time.perf_counter()
        closes_wide = pd.DataFrame({ticker: df["Close"] for ticker, df in price_data.items()})
        rs_results = RelativeStrengthEngine().calculate(closes_wide)
        logger.info("run: Relative Strength complete in %.1fs", time.perf_counter() - stage_start)

        valid_rs_count = sum(
            1 for r in rs_results.values()
            if r.composite_score is not None and r.composite_score == r.composite_score  # NaN != NaN
        )
        valid_rs_fraction = (valid_rs_count / len(rs_results)) if rs_results else 0.0
        if valid_rs_fraction < self.config.min_valid_rs_fraction:
            raise RuntimeError(
                f"run: only {valid_rs_count}/{len(rs_results)} tickers "
                f"({valid_rs_fraction:.0%}) produced a valid Relative Strength "
                f"composite score, below min_valid_rs_fraction="
                f"{self.config.min_valid_rs_fraction:.0%}. Aborting rather than "
                "persisting a degenerate day of all-zero/NaN scores - each of "
                "these tickers already passed min_history_days, so this points "
                "to bad price data from today's fetch, not a real quiet market."
            )

        stage_start = time.perf_counter()
        constituents = self._build_sector_constituents(price_data, sector_map, rs_results)
        sector_scores_list = SectorRotationEngine().rank_sectors(constituents)
        sector_scores_by_name = {s.sector: s for s in sector_scores_list}
        logger.info("run: Sector Rotation complete in %.1fs", time.perf_counter() - stage_start)

        stage_start = time.perf_counter()
        delivery_augmented = self._augment_with_delivery_volume(price_data)
        inst_results = InstitutionalFlowEngine(delivery_augmented).generate_metrics()
        logger.info("run: Institutional Flow complete in %.1fs", time.perf_counter() - stage_start)

        stage_start = time.perf_counter()
        breakout_results = BreakoutEngine(price_data).generate_analysis()
        logger.info("run: Breakout Engine complete in %.1fs", time.perf_counter() - stage_start)

        stage_start = time.perf_counter()
        tickers_with_sector = {t: sector_map.get(t, "OTHER") for t in price_data.keys()}
        ticker_inputs = self.adapter.build_universe_inputs(
            tickers=tickers_with_sector,
            market_context=market_context,
            sector_scores=sector_scores_by_name,
            rs_results=rs_results,
            inst_results=inst_results,
            breakout_results=breakout_results,
        )
        logger.info("run: Integration Adapter complete in %.1fs", time.perf_counter() - stage_start)

        stage_start = time.perf_counter()
        final_results = AdaptiveRanker(ticker_inputs, self.ranker_config).evaluate_universe()
        logger.info("run: Final Ranker complete in %.1fs", time.perf_counter() - stage_start)

        stage_start = time.perf_counter()
        self._persist_results(
            scan_date, market_context, sector_scores_by_name, sector_map,
            rs_results, inst_results, breakout_results, final_results,
        )
        logger.info("run: Analytics Repository persist complete in %.1fs", time.perf_counter() - stage_start)

        if self.config.enable_dashboard:
            master_state = self._build_master_state(
                market_context, sector_scores_list, rs_results, inst_results,
                breakout_results, final_results, sector_map, len(price_data),
            )
            TerminalDashboard(master_state).render_dashboard()

        self.last_run = PipelineRunArtifacts(
            scan_date=scan_date,
            market_context=market_context,
            ranker_config=self.ranker_config,
            sector_scores=sector_scores_by_name,
            rs_results=rs_results,
            inst_results=inst_results,
            breakout_results=breakout_results,
            ticker_inputs=ticker_inputs,
            final_results=final_results,
            sector_map=sector_map,
            universe_requested=list(tickers),
            tickers_skipped_insufficient_history=skipped_tickers,
        )
        if self.config.enable_validation_report:
            report = self.validation_engine.generate_report(self.last_run)
            self.validation_engine.print_report(report, market_context=market_context)

        total_elapsed = time.perf_counter() - run_start
        actionable = sum(1 for r in final_results.values() if r.position_size > 0)
        logger.info(
            "run: pipeline complete in %.1fs — %d ticker(s) scored, %d actionable",
            total_elapsed, len(final_results), actionable,
        )
        return final_results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    PipelineRunner().run()

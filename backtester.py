"""
Lightweight Vectorized Backtesting & Performance Engine (IAS Plug-in Module)

Replays a log of already-closed trades (not live prices), builds a daily
portfolio equity curve per strategy version, and computes institutional-
grade trade- and portfolio-level performance metrics — built specifically
for comparing two strategy iterations (e.g. 'V1.0' vs 'IAS') side by side.

Design notes
------------
- Completely independent: no imports from config.py, data_broker.py,
  scanner_engine.py, or any other IAS module. Pure simulation over a
  supplied trade log — no live trading logic, broker API calls, or
  real-time loops of any kind.
- Not a general-purpose backtesting framework (no order book, no slippage
  model, no intrabar simulation) — it is a purpose-built, vectorized
  Pandas/NumPy engine for the one thing this project needs: turning a
  DataFrame of closed trades into an equity curve and a metrics table.
- Equity-curve assumption: this module only receives trade-level entry/
  exit records, not a daily price panel, so each trade's P&L is modeled as
  realized in full on its `exit_date` (a standard simplification for
  trade-log-only backtests). Overlapping trades on the same exit day are
  summed. This is documented here rather than silently assumed.
- All lookback-free constants (starting capital, risk-free rate, trading
  days per year) live in `BacktestConfig` — nothing hardcoded inline.
- Calculation methods operate on whole Series/DataFrames via vectorized
  Pandas ops. The only loop in the module is over the (typically two)
  `strategy_version` groups in `generate_comparison_report` — an
  unavoidable, tiny outer loop for per-strategy assembly, not part of the
  per-trade math itself.
- No global/module-level mutable state.
- Logs at DEBUG for per-strategy metric values and INFO for the overall
  comparison-report summary, via a module-level `logger` (standard
  `logging` module) — independent of the console table
  `generate_comparison_report` already prints.
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
class StrategyMetrics:
    """Performance summary for one `strategy_version`.

    Attributes:
        strategy_name: The `strategy_version` value this summarizes (e.g.
            'V1.0', 'IAS').
        total_trades: Number of closed trades for this strategy.
        win_rate: Fraction (0.0-1.0) of trades with exit_price > entry_price.
        profit_factor: Sum of gross trade profits / abs(sum of gross trade
            losses). `float('inf')` if there were profits and zero losses;
            0.0 if there were no trades at all.
        cagr: Compound Annual Growth Rate of the equity curve, as a
            fraction (0.05 = 5%/yr).
        max_drawdown: Largest peak-to-trough decline of the equity curve,
            as a positive fraction (0.15 = 15% drawdown).
        sharpe_ratio: (CAGR - risk_free_rate) / annualized volatility.
        sortino_ratio: (CAGR - risk_free_rate) / annualized downside
            deviation.
    """
    strategy_name: str
    total_trades: int
    win_rate: float
    profit_factor: float
    cagr: float
    max_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BacktestConfig:
    """Tunable parameters for PerformanceEngine.

    Attributes:
        starting_capital: Starting portfolio equity for each strategy's
            simulated curve.
        risk_free_rate: Annual risk-free rate (fraction, e.g. 0.06 = 6%)
            used in the Sharpe/Sortino numerator.
        trading_days_per_year: Trading-day count used for annualizing
            volatility (sqrt(trading_days_per_year)).
    """
    starting_capital: float = 100_000.0
    risk_free_rate: float = 0.06
    trading_days_per_year: int = 252


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class PerformanceEngine:
    """Vectorized calculator that turns a closed-trade log into per-
    strategy StrategyMetrics and a console comparison table.

    Example:
        engine = PerformanceEngine(BacktestConfig())
        results = engine.generate_comparison_report(trades_df)
        results['IAS'].sharpe_ratio
    """

    REQUIRED_COLUMNS = (
        "ticker",
        "entry_date",
        "exit_date",
        "entry_price",
        "exit_price",
        "position_size",
        "strategy_version",
    )

    def __init__(self, config: Optional[BacktestConfig] = None):
        """
        Args:
            config: Starting capital / risk-free rate / annualization
                basis to use. Defaults to BacktestConfig() if not
                supplied.
        """
        self.config = config or BacktestConfig()

    # -- trade-level metrics --------------------------------------------------- #

    def calculate_trade_returns_pct(self, trades: pd.DataFrame) -> pd.Series:
        """% return per trade: (exit_price / entry_price - 1) * 100.

        Args:
            trades: Trade rows for one strategy.
        """
        return (trades["exit_price"] / trades["entry_price"] - 1.0) * 100.0

    def calculate_trade_pnl(self, trades: pd.DataFrame) -> pd.Series:
        """Currency P&L per trade: position_size * (exit_price/entry_price - 1).
        `position_size` is interpreted as the capital allocated to that
        trade, in the same currency as `config.starting_capital`.

        Args:
            trades: Trade rows for one strategy.
        """
        return trades["position_size"] * (trades["exit_price"] / trades["entry_price"] - 1.0)

    def calculate_win_rate(self, trades: pd.DataFrame) -> float:
        """Fraction of trades where exit_price > entry_price.

        Args:
            trades: Trade rows for one strategy.
        """
        if trades.empty:
            return 0.0
        wins = trades["exit_price"] > trades["entry_price"]
        return float(wins.mean())

    def calculate_profit_factor(self, pnl: pd.Series) -> float:
        """Sum of gross profits / abs(sum of gross losses).

        Args:
            pnl: Per-trade currency P&L, as returned by
                `calculate_trade_pnl`.

        Returns:
            float('inf') if there are profits and zero losses; 0.0 if
            `pnl` is empty.
        """
        if pnl.empty:
            return 0.0
        gross_profit = float(pnl[pnl > 0].sum())
        gross_loss = float(pnl[pnl < 0].sum())
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / abs(gross_loss)

    def calculate_average_risk_reward(self, returns_pct: pd.Series) -> float:
        """Average winning trade % / abs(average losing trade %).

        Args:
            returns_pct: Per-trade % returns, as returned by
                `calculate_trade_returns_pct`.

        Returns:
            float('inf') if there are wins and zero losing trades; 0.0 if
            `returns_pct` is empty or there are no winning trades.
        """
        if returns_pct.empty:
            return 0.0
        wins = returns_pct[returns_pct > 0]
        losses = returns_pct[returns_pct < 0]
        avg_win = float(wins.mean()) if not wins.empty else 0.0
        avg_loss = float(losses.mean()) if not losses.empty else 0.0
        if avg_loss == 0:
            return float("inf") if avg_win > 0 else 0.0
        return avg_win / abs(avg_loss)

    # -- equity curve ------------------------------------------------------------ #

    def build_equity_curve(self, trades: pd.DataFrame, pnl: pd.Series) -> pd.Series:
        """Builds a daily (business-day) equity curve by realizing each
        trade's full P&L on its `exit_date` and compounding on top of
        `config.starting_capital`.

        Args:
            trades: Trade rows for one strategy (must include `exit_date`).
            pnl: Per-trade currency P&L, aligned to `trades`' index, as
                returned by `calculate_trade_pnl`.

        Returns:
            pd.Series of equity values indexed by business date, spanning
            from the earliest exit_date to the latest. Empty if `trades`
            is empty.
        """
        if trades.empty:
            return pd.Series(dtype=float)

        exit_dates = pd.to_datetime(trades["exit_date"])
        daily_pnl = pnl.groupby(exit_dates).sum()

        full_index = pd.bdate_range(start=daily_pnl.index.min(), end=daily_pnl.index.max())
        daily_pnl = daily_pnl.reindex(full_index, fill_value=0.0)

        return self.config.starting_capital + daily_pnl.cumsum()

    def calculate_cagr(self, equity: pd.Series) -> float:
        """Compound Annual Growth Rate:
        ((Final Equity / Starting Equity) ^ (1 / (Total Days / 365.25))) - 1.

        Args:
            equity: Equity curve, as returned by `build_equity_curve`.

        Returns:
            0.0 if `equity` has fewer than 2 points or spans 0 days.
        """
        if len(equity) < 2:
            return 0.0
        total_days = (equity.index[-1] - equity.index[0]).days
        if total_days <= 0 or equity.iloc[0] <= 0:
            return 0.0
        years = total_days / 365.25
        return float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0)

    def calculate_max_drawdown(self, equity: pd.Series) -> float:
        """Largest peak-to-trough decline: max((RunningPeak - Equity) / RunningPeak).

        Args:
            equity: Equity curve, as returned by `build_equity_curve`.

        Returns:
            Positive fraction (0.15 = 15% drawdown); 0.0 if `equity` is
            empty.
        """
        if equity.empty:
            return 0.0
        running_peak = equity.cummax()
        drawdown = (running_peak - equity) / running_peak
        return float(drawdown.max())

    def _daily_returns(self, equity: pd.Series) -> pd.Series:
        """% daily returns of the equity curve, first (undefined) point
        dropped."""
        if len(equity) < 2:
            return pd.Series(dtype=float)
        return equity.pct_change().dropna()

    def calculate_sharpe_ratio(self, equity: pd.Series, cagr: float) -> float:
        """(CAGR - risk_free_rate) / Annualized Volatility, where
        Annualized Volatility = daily return std * sqrt(trading_days_per_year).

        Args:
            equity: Equity curve, as returned by `build_equity_curve`.
            cagr: This strategy's CAGR, as returned by `calculate_cagr`
                (used as the "Annualized Return" in the numerator).

        Returns:
            0.0 if there isn't enough return history or volatility is 0.
        """
        returns = self._daily_returns(equity)
        if returns.empty:
            return 0.0
        annualized_vol = float(returns.std() * np.sqrt(self.config.trading_days_per_year))
        if annualized_vol == 0:
            return 0.0
        return (cagr - self.config.risk_free_rate) / annualized_vol

    def calculate_sortino_ratio(self, equity: pd.Series, cagr: float) -> float:
        """(CAGR - risk_free_rate) / Downside Deviation, where Downside
        Deviation = std of only negative daily returns * sqrt(trading_days_per_year).

        Args:
            equity: Equity curve, as returned by `build_equity_curve`.
            cagr: This strategy's CAGR, as returned by `calculate_cagr`
                (used as the "Annualized Return" in the numerator).

        Returns:
            0.0 if there isn't enough return history, or no negative
            daily returns (undefined downside risk is treated as 0 risk
            adjustment rather than raising).
        """
        returns = self._daily_returns(equity)
        downside_returns = returns[returns < 0]
        if downside_returns.empty:
            return 0.0
        downside_deviation = float(downside_returns.std() * np.sqrt(self.config.trading_days_per_year))
        if downside_deviation == 0:
            return 0.0
        return (cagr - self.config.risk_free_rate) / downside_deviation

    # -- per-strategy assembly ------------------------------------------------- #

    def evaluate_strategy(self, strategy_name: str, trades: pd.DataFrame) -> Dict[str, float]:
        """Runs every metric for one strategy's trade log.

        Args:
            strategy_name: The `strategy_version` value being evaluated.
            trades: Trade rows for this strategy only.

        Returns:
            Dict with all trade- and portfolio-level metrics, including
            `avg_risk_reward` (computed per spec but not part of the
            returned StrategyMetrics dataclass — surfaced separately here
            for the console comparison table).
        """
        pnl = self.calculate_trade_pnl(trades)
        returns_pct = self.calculate_trade_returns_pct(trades)
        equity = self.build_equity_curve(trades, pnl)

        cagr = self.calculate_cagr(equity)

        metrics = {
            "strategy_name": strategy_name,
            "total_trades": int(len(trades)),
            "win_rate": self.calculate_win_rate(trades),
            "profit_factor": self.calculate_profit_factor(pnl),
            "avg_risk_reward": self.calculate_average_risk_reward(returns_pct),
            "cagr": cagr,
            "max_drawdown": self.calculate_max_drawdown(equity),
            "sharpe_ratio": self.calculate_sharpe_ratio(equity, cagr),
            "sortino_ratio": self.calculate_sortino_ratio(equity, cagr),
        }
        logger.debug("evaluate_strategy: %s -> %s", strategy_name, metrics)
        return metrics

    # -- public API ---------------------------------------------------------- #

    def generate_comparison_report(self, trades_df: pd.DataFrame) -> Dict[str, StrategyMetrics]:
        """Groups `trades_df` by `strategy_version`, computes metrics for
        each, prints a side-by-side ASCII comparison table, and returns
        the results.

        Args:
            trades_df: Closed-trade log with columns `ticker`,
                `entry_date`, `exit_date`, `entry_price`, `exit_price`,
                `position_size`, `strategy_version`.

        Returns:
            Dict mapping strategy_version -> StrategyMetrics.
        """
        results: Dict[str, StrategyMetrics] = {}
        extras: Dict[str, Dict[str, float]] = {}

        if trades_df is None or trades_df.empty:
            logger.warning("generate_comparison_report: received no trades")
            self._print_comparison_table(results, extras)
            return results

        for strategy_name, group in trades_df.groupby("strategy_version"):
            metrics = self.evaluate_strategy(strategy_name, group)
            extras[strategy_name] = metrics
            results[strategy_name] = StrategyMetrics(
                strategy_name=metrics["strategy_name"],
                total_trades=metrics["total_trades"],
                win_rate=metrics["win_rate"],
                profit_factor=metrics["profit_factor"],
                cagr=metrics["cagr"],
                max_drawdown=metrics["max_drawdown"],
                sharpe_ratio=metrics["sharpe_ratio"],
                sortino_ratio=metrics["sortino_ratio"],
            )

        logger.info(
            "generate_comparison_report: compared %d strategy version(s): %s",
            len(results), sorted(results.keys()),
        )
        self._print_comparison_table(results, extras)
        return results

    @staticmethod
    def _fmt(value: float, pct: bool = False) -> str:
        if value == float("inf"):
            return "inf"
        return f"{value:.2%}" if pct else f"{value:.2f}"

    def _print_comparison_table(
        self, results: Dict[str, StrategyMetrics], extras: Dict[str, Dict[str, float]]
    ) -> None:
        """Prints a formatted, side-by-side ASCII table of every
        strategy's metrics."""
        strategies = list(results.keys())
        col_width = 16
        label_width = 22

        print("\n" + "=" * (label_width + col_width * max(len(strategies), 1) + 2))
        print("STRATEGY PERFORMANCE COMPARISON".center(label_width + col_width * max(len(strategies), 1) + 2))
        print("=" * (label_width + col_width * max(len(strategies), 1) + 2))

        if not strategies:
            print("No trades supplied.")
            print("=" * (label_width + 2))
            return

        header = "Metric".ljust(label_width) + "".join(s.rjust(col_width) for s in strategies)
        print(header)
        print("-" * len(header))

        rows = [
            ("Total Trades", lambda m: str(m["total_trades"])),
            ("Win Rate", lambda m: self._fmt(m["win_rate"], pct=True)),
            ("Profit Factor", lambda m: self._fmt(m["profit_factor"])),
            ("Avg Risk/Reward", lambda m: self._fmt(m["avg_risk_reward"])),
            ("CAGR", lambda m: self._fmt(m["cagr"], pct=True)),
            ("Max Drawdown", lambda m: self._fmt(m["max_drawdown"], pct=True)),
            ("Sharpe Ratio", lambda m: self._fmt(m["sharpe_ratio"])),
            ("Sortino Ratio", lambda m: self._fmt(m["sortino_ratio"])),
        ]

        for label, formatter in rows:
            line = label.ljust(label_width)
            for strategy_name in strategies:
                line += formatter(extras[strategy_name]).rjust(col_width)
            print(line)

        print("=" * len(header))

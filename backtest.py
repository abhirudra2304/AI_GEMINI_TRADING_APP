import argparse
import math
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

import config
import pandas as pd


# The scanner stores timestamps without an explicit timezone in older databases.
# NSE candles should be interpreted in India market time unless the value itself
# already carries a timezone offset.
MARKET_TZ = ZoneInfo("Asia/Kolkata")

# Outcomes that are included in performance statistics. Skipped rows are still
# written to SQLite for auditability, but do not pollute win-rate or expectancy.
TERMINAL_OUTCOMES = {
    "WIN",
    "LOSS",
    "TIME_STOP",
    "FORCED_CLOSE",
    "GAP_THROUGH_STOP",
    "GAP_THROUGH_TARGET",
    "SESSION_CLOSE",
}


OUTCOME_COLUMNS = {
    "signal_id": "INTEGER PRIMARY KEY",
    "symbol": "TEXT",
    "signal_timestamp": "TEXT",
    "strategy": "TEXT",
    "planned_entry": "REAL",
    "entry": "REAL",
    "entry_time": "TEXT",
    "stop": "REAL",
    "target": "REAL",
    "outcome": "TEXT",
    "exit_reason": "TEXT",
    "exit_price": "REAL",
    "exit_time": "TEXT",
    "candles_held": "INTEGER",
    "holding_days": "REAL",
    "return": "REAL",
    "return_decimal": "REAL",
    "adj_return_decimal": "REAL",
    "return_pct": "REAL",
    "adj_return_pct": "REAL",
    "friction_decimal": "REAL",
    "data_start": "TEXT",
    "data_end": "TEXT",
    "data_bars": "INTEGER",
    "source_file_count": "INTEGER",
    "created_at": "TEXT",
}


@dataclass
class BacktestResult:
    signal_id: int
    symbol: str
    signal_timestamp: str
    strategy: str
    planned_entry: Optional[float]
    entry: Optional[float]
    entry_time: Optional[str]
    stop: Optional[float]
    target: Optional[float]
    outcome: str
    exit_reason: str
    exit_price: Optional[float]
    exit_time: Optional[str]
    candles_held: int
    holding_days: float
    return_decimal: Optional[float]
    adj_return_decimal: Optional[float]
    return_pct: Optional[float]
    adj_return_pct: Optional[float]
    decision_score: float
    friction_decimal: float
    data_start: Optional[str]
    data_end: Optional[str]
    data_bars: int
    source_file_count: int
    created_at: str


def normalize_timestamp(value) -> pd.Timestamp:
    """Normalize DB and candle timestamps to naive Asia/Kolkata exchange time.

    The database historically stored strings like "2026-06-19 15:22" with no
    timezone. Treating those as UTC would shift signals by 5.5 hours and create
    false exits, so naive values are assumed to already be India market time.
    Timezone-aware values are converted into India market time and then made
    naive so pandas comparisons are consistent.
    """
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid timestamp: {value!r}")
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert(MARKET_TZ).tz_localize(None)
    return ts


def normalize_timestamp_series(series: pd.Series) -> pd.Series:
    """Vectorized timestamp normalization for candle files."""
    ts = pd.to_datetime(series, errors="coerce")
    try:
        if getattr(ts.dt, "tz", None) is not None:
            ts = ts.dt.tz_convert(MARKET_TZ).dt.tz_localize(None)
    except AttributeError:
        # Mixed timezone strings can become object dtype. Fall back to the
        # scalar normalizer so a few odd rows do not poison the full file.
        ts = series.apply(lambda value: normalize_timestamp(value) if pd.notna(value) else pd.NaT)
    return ts


def normalize_strategy(row) -> str:
    """Read strategy from old and new signal schemas, defaulting old rows to SWING."""
    for key in ("strategy", "horizon", "Horizon", "HORIZON"):
        if key in row and pd.notna(row[key]):
            value = str(row[key]).strip().upper()
            if value:
                return value
    return "SWING"


def get_signal_id(row) -> int:
    """Use the signals.id primary key, falling back to SQLite rowid for legacy DBs."""
    for key in ("id", "_rowid"):
        if key in row and pd.notna(row[key]):
            return int(row[key])
    raise ValueError("Signal row does not include id or rowid")


def as_float(row, key: str) -> float:
    value = row[key]
    if pd.isna(value):
        raise ValueError(f"Missing required numeric field: {key}")
    return float(value)


def trade_cost_decimal(strategy: str) -> float:
    """Return a strategy-specific round-trip cost estimate."""
    if strategy.upper() in ["GAP", "BTST"]:
        return config.Backtest.INTRADAY_COST_PCT
    return config.Backtest.DELIVERY_COST_PCT


def recalc_levels_from_actual_entry(
    planned_entry: float,
    actual_entry: float,
    planned_stop: float,
    planned_target: float,
) -> Tuple[float, float]:
    """Re-anchor risk levels to the actual fill while preserving planned R.

    The old replay compared a next-candle/open fill against stop/target levels
    calculated from the signal price. If the actual fill gaps away from the
    signal price, the intended risk/reward is no longer true. This function
    keeps the originally planned rupee risk and reward distances, but applies
    them to the actual executable entry price.
    """
    planned_risk = max(0.01, planned_entry - planned_stop)
    planned_reward = max(0.01, planned_target - planned_entry)
    return actual_entry - planned_risk, actual_entry + planned_reward


def decision_score_from_row(row) -> float:
    """Final selection score calibrated from realized outcome diagnostics.
    
    NOTE: This logic MUST be kept in sync with orchestrator.add_decision_scores.
    
    This score was derived from historical analysis which showed that the
    "hottest" signals (high raw score, high RS) were often over-extended and
    performed worse. This score therefore ranks less crowded, confirmed setups
    higher by rewarding lower raw scores and lower relative strength.
    """
    score_val = float(row.get("score", 0) or 0)
    rs_val = float(row.get("rs_pctl", 0) or 0)
    sector_val = float(row.get("sector_rs", 0) or 0)
    adx_val = float(row.get("adx", 0) or 0)
    score_cool = 100 - min(max(score_val, 0), 100)
    rs_cool = 100 - min(max(rs_val, 0), 100)
    sector_cool = 100 - min(max(sector_val, 0), 100)
    adx_cool = 100 - (min(max(adx_val, 0), 50) / 50 * 100)
    return round(
        (rs_cool * 0.40) + (score_cool * 0.35) + (sector_cool * 0.15) + (adx_cool * 0.10),
        1,
    )


def iso_or_none(value) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).isoformat(sep=" ", timespec="seconds")


def ensure_signals_schema(conn: sqlite3.Connection) -> None:
    """Migrate the input table just enough for backward-compatible reads."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
    if "strategy" not in columns:
        conn.execute("ALTER TABLE signals ADD COLUMN strategy TEXT")
    conn.execute(
        "UPDATE signals SET strategy = 'SWING' "
        "WHERE strategy IS NULL OR TRIM(strategy) = ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_signals_symbol_time "
        "ON signals(symbol, timestamp)"
    )
    conn.commit()


def ensure_outcomes_schema(conn: sqlite3.Connection) -> None:
    """Create and migrate outcomes without breaking the old three-column table.

    Older code created outcomes(signal_id, outcome, return). SQLite can add
    nullable columns cheaply, so this migration keeps that table and extends it
    into a full audit trail instead of dropping historical results.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outcomes (
            signal_id INTEGER PRIMARY KEY,
            outcome TEXT,
            return REAL
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(outcomes)")}
    for column, col_type in OUTCOME_COLUMNS.items():
        if column not in existing:
            conn.execute(f'ALTER TABLE outcomes ADD COLUMN "{column}" {col_type}')
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outcomes_strategy_exit "
        "ON outcomes(strategy, exit_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outcomes_symbol_signal "
        "ON outcomes(symbol, signal_timestamp)"
    )
    conn.commit()


def load_signals(conn: sqlite3.Connection, limit: Optional[int] = None) -> pd.DataFrame:
    """Load signals in event order. The ORDER BY makes equity curves reproducible."""
    limit_clause = f" LIMIT {int(limit)}" if limit else ""
    query = (
        "SELECT rowid AS _rowid, * FROM signals "
        "ORDER BY timestamp ASC, symbol ASC, rowid ASC"
        f"{limit_clause}"
    )
    return pd.read_sql_query(query, conn)


class CandleStore:
    """Immutable parquet-backed candle store for backtesting.

    This class intentionally does not call DataBroker.fetch_ohlcv(). The scanner
    broker is a live read-through cache with a short intraday TTL; using it here
    would make the backtest dependent on today's API state and can silently skip
    or refetch historical data. A backtest should replay a fixed dataset.
    """

    def __init__(self, cache_dir: str = "historical_data", interval: str = "FIFTEEN_MINUTE"):
        self.cache_dir = Path(cache_dir)
        self.interval = interval
        self._cache: Dict[str, pd.DataFrame] = {}
        self._meta: Dict[str, Dict[str, object]] = {}

    def _files_for_symbol(self, symbol: str) -> Iterable[Path]:
        pattern = f"{symbol}_{self.interval}*.parquet"
        return sorted(
            self.cache_dir.glob(pattern),
            key=lambda path: (path.stat().st_mtime, path.name),
        )

    def load_symbol(self, symbol: str) -> pd.DataFrame:
        """Load, merge, de-duplicate, and sort all cached candles for one symbol."""
        symbol = str(symbol).strip().upper()
        if symbol in self._cache:
            return self._cache[symbol]

        frames = []
        files = list(self._files_for_symbol(symbol))
        for file_path in files:
            try:
                frame = pd.read_parquet(file_path)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not read {file_path}. Install pyarrow/fastparquet "
                    "or regenerate the cache."
                ) from exc

            if frame.empty or "Timestamp" not in frame.columns:
                continue

            frame = frame.copy()
            frame["Timestamp"] = normalize_timestamp_series(frame["Timestamp"])
            frame = frame.dropna(subset=["Timestamp"])
            keep_cols = ["Timestamp", "Open", "High", "Low", "Close", "Volume"]
            frame = frame[[col for col in keep_cols if col in frame.columns]]
            for col in ("Open", "High", "Low", "Close", "Volume"):
                if col in frame.columns:
                    frame[col] = pd.to_numeric(frame[col], errors="coerce")
            frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
            frames.append(frame)

        if frames:
            candles = pd.concat(frames, ignore_index=True)
            candles = candles.sort_values("Timestamp")
            candles = candles.drop_duplicates(subset=["Timestamp"], keep="last")
            candles = candles.reset_index(drop=True)
        else:
            candles = pd.DataFrame(columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])

        self._cache[symbol] = candles
        self._meta[symbol] = {
            "file_count": len(files),
            "data_start": iso_or_none(candles["Timestamp"].iloc[0]) if not candles.empty else None,
            "data_end": iso_or_none(candles["Timestamp"].iloc[-1]) if not candles.empty else None,
            "data_bars": int(len(candles)),
        }
        return candles

    def meta(self, symbol: str) -> Dict[str, object]:
        self.load_symbol(symbol)
        return self._meta[str(symbol).strip().upper()]

    def future_after(self, symbol: str, signal_time: pd.Timestamp, max_candles: int) -> pd.DataFrame:
        """Return at most max_candles strictly after the signal event time."""
        candles = self.load_symbol(symbol)
        if candles.empty:
            return candles
        timestamps = candles["Timestamp"]
        start_idx = timestamps.searchsorted(signal_time, side="right")
        return candles.iloc[start_idx:start_idx + max_candles].copy()

    def next_session_after(self, symbol: str, signal_time: pd.Timestamp, max_candles: int) -> pd.DataFrame:
        """Return the first cached trading session after the signal date.

        GAP trades are intended to be next-session trades. Entering immediately
        after the signal would overstate a gap setup by using same-session data.
        """
        future = self.future_after(symbol, signal_time, max_candles * 4)
        if future.empty:
            return future
        next_sessions = future[future["Timestamp"].dt.date > signal_time.date()]
        if next_sessions.empty:
            return next_sessions
        first_date = next_sessions["Timestamp"].dt.date.iloc[0]
        session = next_sessions[next_sessions["Timestamp"].dt.date == first_date]
        return session.head(max_candles).copy()


def skipped_result(row, reason: str, store: CandleStore) -> BacktestResult:
    """Write skipped signals too, so data coverage failures are visible in SQL."""
    symbol = str(row.get("symbol", "")).strip().upper()
    signal_time = normalize_timestamp(row["timestamp"])
    strategy = normalize_strategy(row)
    try:
        meta = store.meta(symbol) if symbol else {}
    except Exception:
        meta = {}
    return BacktestResult(
        signal_id=get_signal_id(row),
        symbol=symbol,
        signal_timestamp=iso_or_none(signal_time),
        strategy=strategy,
        planned_entry=float(row["entry"]) if "entry" in row and pd.notna(row["entry"]) else None,
        entry=None,
        entry_time=None,
        stop=float(row["stop"]) if "stop" in row and pd.notna(row["stop"]) else None,
        target=float(row["target"]) if "target" in row and pd.notna(row["target"]) else None,
        outcome=f"SKIPPED_{reason}",
        exit_reason=reason,
        exit_price=None,
        exit_time=None,
        candles_held=0,
        holding_days=0.0,
        return_decimal=None,
        adj_return_decimal=None,
        return_pct=None,
        adj_return_pct=None,
        decision_score=decision_score_from_row(row),
        friction_decimal=trade_cost_decimal(strategy),
        data_start=meta.get("data_start"),
        data_end=meta.get("data_end"),
        data_bars=int(meta.get("data_bars", 0) or 0),
        source_file_count=int(meta.get("file_count", 0) or 0),
        created_at=datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
    )


def finalize_result(
    row,
    store: CandleStore,
    strategy: str,
    path: pd.DataFrame,
    entry_price: float,
    entry_time: pd.Timestamp,
    planned_entry: float,
    stop: float,
    target: float,
    decision_score: float,
    outcome: str,
    exit_reason: str,
    exit_price: float,
    exit_time: pd.Timestamp,
    candles_held: int,
) -> BacktestResult:
    """Create a normalized result row from a strategy evaluator."""
    symbol = str(row["symbol"]).strip().upper()
    signal_time = normalize_timestamp(row["timestamp"])
    raw_return = (exit_price / entry_price) - 1.0
    friction = trade_cost_decimal(strategy)
    adjusted_return = raw_return - friction
    meta = store.meta(symbol)

    return BacktestResult(
        signal_id=get_signal_id(row),
        symbol=symbol,
        signal_timestamp=iso_or_none(signal_time),
        strategy=strategy,
        planned_entry=round(planned_entry, 4),
        entry=round(entry_price, 4),
        entry_time=iso_or_none(entry_time),
        stop=round(stop, 4),
        target=round(target, 4),
        outcome=outcome,
        exit_reason=exit_reason,
        exit_price=round(exit_price, 4),
        exit_time=iso_or_none(exit_time),
        candles_held=int(candles_held),
        holding_days=round(candles_held / config.Backtest.SESSION_CANDLES_15M, 4),
        return_decimal=round(raw_return, 8),
        adj_return_decimal=round(adjusted_return, 8),
        return_pct=round(raw_return * 100, 4),
        adj_return_pct=round(adjusted_return * 100, 4),
        decision_score=decision_score,
        friction_decimal=friction,
        data_start=meta.get("data_start"),
        data_end=meta.get("data_end"),
        data_bars=int(meta.get("data_bars", 0) or 0),
        source_file_count=int(meta.get("file_count", 0) or 0),
        created_at=datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
    )


def evaluate_long_path(
    path: pd.DataFrame,
    entry_price: float,
    stop: float,
    target: float,
    time_exit_label: str,
) -> Tuple[str, str, float, pd.Timestamp, int]:
    """Evaluate a long trade path with conservative same-candle sequencing."""
    for i, candle in enumerate(path.itertuples(index=False), start=1):
        open_price = float(candle.Open)
        low_price = float(candle.Low)
        high_price = float(candle.High)
        close_price = float(candle.Close)
        candle_time = candle.Timestamp

        # If the first tradable price gaps through a stop or target, use the
        # open. This avoids pretending a planned stop filled at a better price
        # than the market actually offered.
        if i == 1 and open_price <= stop:
            return "GAP_THROUGH_STOP", "entry_open_below_stop", open_price, candle_time, i
        if i == 1 and open_price >= target:
            return "GAP_THROUGH_TARGET", "entry_open_above_target", open_price, candle_time, i

        # Conservative intrabar ordering: stop wins if stop and target are both
        # inside the same 15-minute candle. Without tick data this is the safer
        # assumption for a long-only scanner.
        if low_price <= stop:
            return "LOSS", "stop_hit", stop, candle_time, i
        if high_price >= target:
            return "WIN", "target_hit", target, candle_time, i

        if i == len(path):
            return time_exit_label, "time_exit", close_price, candle_time, i

    raise ValueError("Cannot evaluate an empty candle path")


def evaluate_btst(row, store: CandleStore) -> BacktestResult:
    """BTST: enter next available candle, exit by next-session horizon."""
    symbol = str(row["symbol"]).strip().upper()
    signal_time = normalize_timestamp(row["timestamp"])
    path = store.future_after(symbol, signal_time, config.Backtest.HOLDING_PERIOD_CANDLES["BTST"])
    if path.empty:
        return skipped_result(row, "NO_FUTURE_CANDLES", store)

    planned_entry = as_float(row, "entry")
    planned_stop = as_float(row, "stop")
    planned_target = as_float(row, "target")
    entry_time = path["Timestamp"].iloc[0]
    entry_price = float(path["Open"].iloc[0])
    if entry_price <= planned_stop:
        return finalize_result(
            row, store, "BTST", path, entry_price, entry_time, planned_entry,
            planned_stop, planned_target, decision_score_from_row(row),
            "GAP_THROUGH_STOP", "overnight_open_below_planned_stop",
            entry_price, entry_time, 1
        )
    stop, target = recalc_levels_from_actual_entry(
        planned_entry, entry_price, planned_stop, planned_target
    )
    outcome, reason, exit_price, exit_time, held = evaluate_long_path(
        path, entry_price, stop, target, "TIME_STOP"
    )
    return finalize_result(
        row, store, "BTST", path, entry_price, entry_time, planned_entry,
        stop, target, decision_score_from_row(row), outcome, reason,
        exit_price, exit_time, held
    )


def evaluate_gap(row, store: CandleStore) -> BacktestResult:
    """GAP: enter the first candle of the next trading session, exit that session."""
    symbol = str(row["symbol"]).strip().upper()
    signal_time = normalize_timestamp(row["timestamp"])
    path = store.next_session_after(symbol, signal_time, config.Backtest.HOLDING_PERIOD_CANDLES["GAP"])
    if path.empty:
        return skipped_result(row, "NO_NEXT_SESSION_CANDLES", store)

    planned_entry = as_float(row, "entry")
    planned_stop = as_float(row, "stop")
    planned_target = as_float(row, "target")
    entry_time = path["Timestamp"].iloc[0]
    entry_price = float(path["Open"].iloc[0])
    if entry_price <= planned_stop:
        return finalize_result(
            row, store, "GAP", path, entry_price, entry_time, planned_entry,
            planned_stop, planned_target, decision_score_from_row(row),
            "GAP_THROUGH_STOP", "next_session_open_below_planned_stop",
            entry_price, entry_time, 1
        )
    stop, target = recalc_levels_from_actual_entry(
        planned_entry, entry_price, planned_stop, planned_target
    )
    outcome, reason, exit_price, exit_time, held = evaluate_long_path(
        path, entry_price, stop, target, "SESSION_CLOSE"
    )
    return finalize_result(
        row, store, "GAP", path, entry_price, entry_time, planned_entry,
        stop, target, decision_score_from_row(row), outcome, reason,
        exit_price, exit_time, held
    )


def evaluate_swing(row, store: CandleStore) -> BacktestResult:
    """SWING: enter next available candle, hold up to five trading sessions."""
    symbol = str(row["symbol"]).strip().upper()
    signal_time = normalize_timestamp(row["timestamp"])
    path = store.future_after(symbol, signal_time, config.Backtest.HOLDING_PERIOD_CANDLES["SWING"])
    if path.empty:
        return skipped_result(row, "NO_FUTURE_CANDLES", store)

    planned_entry = as_float(row, "entry")
    planned_stop = as_float(row, "stop")
    planned_target = as_float(row, "target")
    entry_time = path["Timestamp"].iloc[0]
    entry_price = float(path["Open"].iloc[0])
    stop, target = recalc_levels_from_actual_entry(
        planned_entry, entry_price, planned_stop, planned_target
    )
    outcome, reason, exit_price, exit_time, held = evaluate_long_path(
        path, entry_price, stop, target, "TIME_STOP"
    )
    return finalize_result(
        row, store, "SWING", path, entry_price, entry_time, planned_entry,
        stop, target, decision_score_from_row(row), outcome, reason,
        exit_price, exit_time, held
    )


def evaluate_signal(row, store: CandleStore) -> BacktestResult:
    """Dispatch to separate strategy logic instead of one generic holding period."""
    strategy = normalize_strategy(row)
    if strategy == "BTST":
        return evaluate_btst(row, store)
    if strategy == "GAP":
        return evaluate_gap(row, store)
    return evaluate_swing(row, store)


def result_to_db_row(result: BacktestResult) -> Dict[str, object]:
    row = asdict(result)
    row["return"] = row["return_decimal"]
    return row


def save_outcomes(conn: sqlite3.Connection, results: Iterable[BacktestResult]) -> None:
    """Upsert all outcomes in one transaction for 1000+ signal efficiency."""
    rows = [result_to_db_row(result) for result in results]
    if not rows:
        return

    columns = list(OUTCOME_COLUMNS)
    placeholders = ", ".join(["?"] * len(columns))
    quoted_cols = ", ".join(f'"{col}"' for col in columns)
    update_cols = [col for col in columns if col != "signal_id"]
    update_sql = ", ".join(f'"{col}" = excluded."{col}"' for col in update_cols)
    sql = (
        f"INSERT INTO outcomes ({quoted_cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(signal_id) DO UPDATE SET {update_sql}"
    )
    values = [[row.get(col) for col in columns] for row in rows]
    with conn:
        conn.executemany(sql, values)


def compute_stats(df: pd.DataFrame, label: str) -> Dict[str, float]:
    """Compute trade-level stats with sane edge-case handling."""
    terminal = df[df["outcome"].isin(TERMINAL_OUTCOMES)].copy()
    terminal = terminal.dropna(subset=["adj_return_decimal"])
    if terminal.empty:
        print(f"\n{label}: No completed trades to report.")
        return {}

    # Sort by exit time for a deterministic sequential trade equity curve. This
    # is not a full portfolio simulator for overlapping trades, but it is less
    # misleading than using arbitrary database order.
    terminal["exit_time_sort"] = pd.to_datetime(terminal["exit_time"], errors="coerce")
    terminal = terminal.sort_values(["exit_time_sort", "signal_timestamp", "symbol"])

    returns = terminal["adj_return_decimal"].astype(float)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    breakeven = returns[returns == 0]

    total = len(returns)
    win_rate = len(wins) / total if total else 0.0
    loss_rate = len(losses) / total if total else 0.0
    avg_win = wins.mean() if not wins.empty else 0.0
    avg_loss = losses.mean() if not losses.empty else 0.0
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0

    expectancy = returns.mean()
    formula_expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)

    equity = (1.0 + returns).cumprod()
    equity = pd.concat([pd.Series([1.0]), equity], ignore_index=True)
    running_max = equity.cummax()
    drawdown = (equity / running_max) - 1.0
    max_dd = drawdown.min()

    pf_text = "inf" if math.isinf(profit_factor) else f"{profit_factor:.2f}"
    print("\n" + "=" * 72)
    print(f"{label} REPORT")
    print("=" * 72)
    print(f"Trades             : {total}")
    print(f"Wins / Losses / BE : {len(wins)} / {len(losses)} / {len(breakeven)}")
    print(f"Win Rate           : {win_rate * 100:.2f}%")
    print(f"Average Win        : {avg_win * 100:.2f}%")
    print(f"Average Loss       : {avg_loss * 100:.2f}%")
    print(f"Profit Factor      : {pf_text}")
    print(f"Expectancy         : {expectancy * 100:.2f}%")
    print(f"Formula Expectancy : {formula_expectancy * 100:.2f}%")
    print(f"Max Drawdown       : {max_dd * 100:.2f}%")
    print("=" * 72)

    return {
        "trades": float(total),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "max_drawdown": max_dd,
    }


def compute_topn_portfolio_stats(df: pd.DataFrame, top_n: int, label: str) -> Dict[str, float]:
    """Validate the manual workflow: take only the top N signals per day.

    This is the equity curve that matters for a trader taking 1-2 ideas, not a
    synthetic curve that compounds every historical signal as if unlimited
    capital existed. Returns are equal-weighted across selected trades on each
    signal date, then compounded one decision day at a time.
    """
    terminal = df[df["outcome"].isin(TERMINAL_OUTCOMES)].copy()
    terminal = terminal.dropna(subset=["adj_return_decimal", "decision_score"])
    if terminal.empty:
        print(f"\n{label}: No completed trades to report.")
        return {}

    terminal["signal_day"] = pd.to_datetime(
        terminal["signal_timestamp"], errors="coerce"
    ).dt.date
    terminal = terminal.dropna(subset=["signal_day"])
    terminal = terminal.sort_values(
        ["signal_day", "strategy", "decision_score", "symbol"],
        ascending=[True, True, False, True],
    )

    selected = terminal.groupby(["signal_day", "strategy"], group_keys=False).head(top_n)
    daily_returns = selected.groupby("signal_day")["adj_return_decimal"].mean().astype(float)
    if daily_returns.empty:
        print(f"\n{label}: No selected trades to report.")
        return {}

    wins = selected[selected["adj_return_decimal"] > 0]
    losses = selected[selected["adj_return_decimal"] < 0]
    gross_profit = selected.loc[selected["adj_return_decimal"] > 0, "adj_return_decimal"].sum()
    gross_loss = abs(selected.loc[selected["adj_return_decimal"] < 0, "adj_return_decimal"].sum())
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0

    equity = (1.0 + daily_returns).cumprod()
    equity = pd.concat([pd.Series([1.0]), equity], ignore_index=True)
    drawdown = (equity / equity.cummax()) - 1.0
    max_dd = drawdown.min()
    expectancy = selected["adj_return_decimal"].mean()
    win_rate = len(wins) / len(selected) if len(selected) else 0.0
    pf_text = "inf" if math.isinf(profit_factor) else f"{profit_factor:.2f}"

    print("\n" + "=" * 72)
    print(f"{label} TOP-{top_n} DAILY PORTFOLIO REPORT")
    print("=" * 72)
    print(f"Decision Days      : {len(daily_returns)}")
    print(f"Selected Trades    : {len(selected)}")
    print(f"Win Rate           : {win_rate * 100:.2f}%")
    print(f"Profit Factor      : {pf_text}")
    print(f"Expectancy/Trade   : {expectancy * 100:.2f}%")
    print(f"Expectancy/Day     : {daily_returns.mean() * 100:.2f}%")
    print(f"Max Drawdown       : {max_dd * 100:.2f}%")
    print("=" * 72)

    return {
        "decision_days": float(len(daily_returns)),
        "selected_trades": float(len(selected)),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "daily_expectancy": daily_returns.mean(),
        "max_drawdown": max_dd,
    }


def print_bias_audit() -> None:
    """Surface the remaining research limitations every time the report runs."""
    print("\nBIAS AUDIT")
    print("- Look-ahead: fixed in this backtest by replaying cached candles strictly after each signal timestamp.")
    print("- Future leakage: fixed for exits; no live broker fetch is used by default in backtest.")
    print("- Survivorship: CRITICAL FLAW. Signals were generated from today's scanner universe, not a point-in-time universe. Results are likely overly optimistic.")
    print("- Timestamp: older signals are timezone-naive and assumed to be Asia/Kolkata.")
    print("- Corporate Actions: CRITICAL FLAW. Backtest does not handle stock splits/dividends. A split between signal generation and backtest execution will invalidate results for that symbol.")
    print("- Data coverage: skipped outcomes are stored in SQLite when no post-signal candles exist.")


def run_backtest_report(
    db_path: str = "signals.db",
    cache_dir: str = "historical_data",
    limit: Optional[int] = None,
) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        ensure_signals_schema(conn)
        ensure_outcomes_schema(conn)
        signals = load_signals(conn, limit=limit)
        if signals.empty:
            print("No signals found in database.")
            return pd.DataFrame()

        print_bias_audit()
        print(f"\nAnalyzing {len(signals)} historical signals from {db_path}...")

        store = CandleStore(cache_dir=cache_dir)
        results = []
        for _, row in signals.iterrows():
            try:
                results.append(evaluate_signal(row, store))
            except Exception as exc:
                # Bad rows are persisted as skipped rows rather than disappearing
                # from the denominator of data-quality analysis.
                skipped = skipped_result(row, f"ERROR_{type(exc).__name__}", store)
                skipped.exit_reason = str(exc)[:250]
                results.append(skipped)

        save_outcomes(conn, results)
    finally:
        conn.close()

    results_df = pd.DataFrame([result_to_db_row(result) for result in results])
    terminal_count = int(results_df["outcome"].isin(TERMINAL_OUTCOMES).sum())
    skipped_count = len(results_df) - terminal_count

    compute_stats(results_df, "OVERALL")
    for strat in ["BTST", "GAP", "SWING"]:
        compute_stats(results_df[results_df["strategy"] == strat].copy(), strat)
    for top_n in (1, 3, 5):
        compute_topn_portfolio_stats(results_df, top_n, "BTST/SWING/GAP")

    results_df.to_csv("backtest_results.csv", index=False)
    print("\nSaved: backtest_results.csv")
    print("Saved outcomes to SQLite table: outcomes")
    print(f"Completed trades: {terminal_count}")
    print(f"Skipped/audit rows: {skipped_count}")
    return results_df


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay scanner signals against cached historical candles.")
    parser.add_argument("--db", default="signals.db", help="SQLite database containing signals/outcomes.")
    parser.add_argument("--cache-dir", default="historical_data", help="Directory containing cached parquet candles.")
    parser.add_argument("--limit", type=int, default=None, help="Optional signal limit for smoke tests.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_backtest_report(db_path=args.db, cache_dir=args.cache_dir, limit=args.limit)

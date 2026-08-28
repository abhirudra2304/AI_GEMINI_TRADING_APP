"""
Analytics Repository (IAS Plug-in Module)

Pure SQLite data-access layer (Repository Pattern) for logging daily
scanner outputs and, later, backfilling their forward returns — the
persistence layer a future ML training pipeline reads from.

Design notes
------------
- Completely independent: no imports from config.py, data_broker.py,
  scanner_engine.py, or any of the other IAS modules. Nothing in the
  existing codebase imports this one, and this module has no scanner
  business logic of its own — it only stores and retrieves whatever
  scalar values it's given.
- Pure infrastructure: no calculations, no trading rules, no scoring. If
  you find yourself wanting to compute something here, it belongs
  upstream in the module that produced the score.
- All SQL is parameterized (`?` placeholders) — no f-string/format
  interpolation of caller-supplied values into a query. The one place a
  dict's *keys* (not values) influence generated SQL
  (`update_forward_returns`) validates every key against a fixed
  whitelist before it's used to build the statement.
- Composite primary key `(scan_date, ticker)` plus `INSERT OR REPLACE`
  makes `log_daily_scans` idempotent — re-running the scanner on the same
  day overwrites that day's rows instead of duplicating them.
- No global/module-level mutable state. All state is the `db_path` held
  by an AnalyticsRepository instance; each operation opens its own
  short-lived connection rather than holding one open across calls.
- Logs at INFO for every write (row counts, affected keys) and DEBUG for
  reads, via a module-level `logger` (standard `logging` module) — useful
  for auditing what got persisted without needing to inspect the SQLite
  file directly.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data Transfer Object
# --------------------------------------------------------------------------- #

@dataclass
class ScanRecord:
    """One ticker's point-in-time scan result for one scan day.

    Attributes:
        scan_date: Date the scan was run, 'YYYY-MM-DD'. Part of the
            composite primary key.
        ticker: Stock ticker. Part of the composite primary key.
        scanner_score: Overall scanner/composite score for the day.
        sector_score: Sector-rotation score at scan time.
        rs_score: Relative-strength score at scan time.
        inst_score: Institutional-flow score at scan time.
        breakout_score: Breakout-pattern confidence at scan time.
        alpha_score: Final regime-adjusted alpha score at scan time.
        market_regime: Market regime label at scan time (e.g.
            'TRENDING_BULL').
        is_selected: Whether the system actually flagged this ticker as a
            buy that day.
        ret_1d, ret_3d, ret_5d, ret_10d: Forward returns. Left as None
            (stored as NULL) at scan time; filled in later via
            `update_forward_returns`.
        stop_hit, target_hit: Whether the stop-loss / target was hit
            during the forward window. Left as None (NULL) at scan time.
    """
    scan_date: str
    ticker: str
    scanner_score: float
    sector_score: float
    rs_score: float
    inst_score: float
    breakout_score: float
    alpha_score: float
    market_regime: str
    is_selected: bool
    ret_1d: Optional[float] = None
    ret_3d: Optional[float] = None
    ret_5d: Optional[float] = None
    ret_10d: Optional[float] = None
    stop_hit: Optional[bool] = None
    target_hit: Optional[bool] = None


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #

class AnalyticsRepository:
    """SQLite-backed repository for `scanner_analytics`.

    Example:
        repo = AnalyticsRepository("analytics.db")
        repo.log_daily_scans([ScanRecord(...), ScanRecord(...)])
        repo.update_forward_returns("2026-07-22", "HAL.NS", {"ret_1d": 1.4})
        rows = repo.fetch_training_data("2026-01-01", "2026-07-22")
    """

    TABLE_NAME = "scanner_analytics"

    # Columns `update_forward_returns` is allowed to write. Validated as a
    # whitelist before building any UPDATE statement, since those keys (not
    # just their values) feed into the generated SQL.
    _UPDATABLE_RETURN_COLUMNS = frozenset(
        {"ret_1d", "ret_3d", "ret_5d", "ret_10d", "stop_hit", "target_hit"}
    )

    def __init__(self, db_path: str):
        """
        Args:
            db_path: Filesystem path to the SQLite database file. Created
                on first use if it doesn't exist.
        """
        self.db_path = db_path
        self._create_tables()
        logger.debug("AnalyticsRepository initialized (db_path=%s)", db_path)

    # -- connection handling ------------------------------------------------ #

    @contextmanager
    def _connect(self):
        """Yields a short-lived sqlite3 connection with row access by
        column name, committing on clean exit and always closing."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- schema --------------------------------------------------------------- #

    def _create_tables(self) -> None:
        """Creates the `scanner_analytics` table (and its primary key
        index) if it doesn't already exist. Safe to call repeatedly."""
        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    scan_date       TEXT    NOT NULL,
                    ticker          TEXT    NOT NULL,
                    scanner_score   REAL,
                    sector_score    REAL,
                    rs_score        REAL,
                    inst_score      REAL,
                    breakout_score  REAL,
                    alpha_score     REAL,
                    market_regime   TEXT,
                    is_selected     INTEGER,
                    ret_1d          REAL,
                    ret_3d          REAL,
                    ret_5d          REAL,
                    ret_10d         REAL,
                    stop_hit        INTEGER,
                    target_hit      INTEGER,
                    PRIMARY KEY (scan_date, ticker)
                )
                """
            )

    # -- writes --------------------------------------------------------------- #

    def log_daily_scans(self, records: List[ScanRecord]) -> None:
        """Batch-upserts a day's (or multiple days') scan results.

        Uses `INSERT OR REPLACE`, so re-logging a `(scan_date, ticker)`
        that already exists overwrites that row rather than creating a
        duplicate — this is what makes re-running the scanner mid-day
        safe.

        Args:
            records: ScanRecord rows to persist. No-op if empty.
        """
        if not records:
            logger.debug("log_daily_scans: called with no records, skipping")
            return

        rows = [
            (
                r.scan_date,
                r.ticker,
                r.scanner_score,
                r.sector_score,
                r.rs_score,
                r.inst_score,
                r.breakout_score,
                r.alpha_score,
                r.market_regime,
                int(bool(r.is_selected)),
                r.ret_1d,
                r.ret_3d,
                r.ret_5d,
                r.ret_10d,
                None if r.stop_hit is None else int(bool(r.stop_hit)),
                None if r.target_hit is None else int(bool(r.target_hit)),
            )
            for r in records
        ]

        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_NAME} (
                    scan_date, ticker, scanner_score, sector_score, rs_score,
                    inst_score, breakout_score, alpha_score, market_regime,
                    is_selected, ret_1d, ret_3d, ret_5d, ret_10d, stop_hit,
                    target_hit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        logger.info("log_daily_scans: upserted %d row(s) into %s", len(rows), self.TABLE_NAME)

    def update_forward_returns(
        self, scan_date: str, ticker: str, returns_data: Dict[str, Any]
    ) -> None:
        """Backfills forward-return / outcome columns for one existing
        `(scan_date, ticker)` row once the future price action is known.

        Args:
            scan_date: Scan date of the row to update.
            ticker: Ticker of the row to update.
            returns_data: Mapping of column name -> new value. Keys must
                be a subset of {'ret_1d', 'ret_3d', 'ret_5d', 'ret_10d',
                'stop_hit', 'target_hit'}; 'stop_hit'/'target_hit' values
                are coerced to 0/1.

        Raises:
            ValueError: If `returns_data` is empty or contains a key
                outside the updatable-columns whitelist.
        """
        if not returns_data:
            raise ValueError("update_forward_returns requires at least one field to update")

        unknown = set(returns_data) - self._UPDATABLE_RETURN_COLUMNS
        if unknown:
            logger.warning(
                "update_forward_returns: rejected disallowed column(s) %s for %s/%s",
                sorted(unknown), scan_date, ticker,
            )
            raise ValueError(
                f"Unknown/disallowed forward-return column(s): {sorted(unknown)}. "
                f"Allowed: {sorted(self._UPDATABLE_RETURN_COLUMNS)}"
            )

        columns = list(returns_data.keys())
        values = [
            int(bool(returns_data[c])) if c in ("stop_hit", "target_hit") else returns_data[c]
            for c in columns
        ]
        set_clause = ", ".join(f"{c} = ?" for c in columns)

        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {self.TABLE_NAME}
                SET {set_clause}
                WHERE scan_date = ? AND ticker = ?
                """,
                (*values, scan_date, ticker),
            )
        logger.info(
            "update_forward_returns: updated %s for %s/%s", sorted(returns_data.keys()), scan_date, ticker
        )

    # -- reads --------------------------------------------------------------- #

    def fetch_training_data(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """Retrieves scan records (scores + whatever outcomes have been
        backfilled so far) for an inclusive date range, ready to hand to
        `pd.DataFrame(rows)` for an ML training pipeline.

        Args:
            start_date: Inclusive range start, 'YYYY-MM-DD'.
            end_date: Inclusive range end, 'YYYY-MM-DD'.

        Returns:
            List of plain dicts, one per row, ordered by
            (scan_date, ticker). Rows whose forward returns haven't been
            backfilled yet simply have None for those fields.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                SELECT * FROM {self.TABLE_NAME}
                WHERE scan_date BETWEEN ? AND ?
                ORDER BY scan_date, ticker
                """,
                (start_date, end_date),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        logger.debug("fetch_training_data: fetched %d row(s) for %s..%s", len(rows), start_date, end_date)
        return rows

import sqlite3
import json
import logging
import pandas as pd
from datetime import datetime

import config
from lifecycle_manager import shutdown_manager

logger = logging.getLogger(__name__)

class SignalDB:
    def __init__(self, db_path: str = 'signals.db'):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        # 2026-08-21: every scan path (momentum, emfb, orchestrator, historical
        # generator) instantiates its own SignalDB() and writes to the same
        # signals.db/reports.db file - under SQLite's default rollback-journal
        # mode a writer blocks all readers/writers, so overlapping runs (e.g.
        # a manual scan still finishing when Prewarm/EOD starts) risk a
        # "database is locked" exception on whichever one loses the race.
        # WAL lets readers and a writer proceed concurrently, and the
        # busy_timeout makes any remaining brief contention retry instead of
        # failing immediately. WAL mode is stored in the db file itself, so
        # this only needs to run once per file, but it's cheap to set here
        # every time (a no-op after the first).
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._init_db()
        shutdown_manager.register(self.close)

    def __del__(self):
        self.close()

    def _get_existing_columns(self, table_name: str) -> set:
        """Gets the set of existing column names for a table."""
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}

    def _init_db(self):
        cursor = self.conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME, symbol TEXT, strategy TEXT,
            score REAL, rs_pctl REAL, sector_rs REAL, adx REAL, vol_ratio REAL, entry REAL, stop REAL, target REAL,
            EMFB_Score REAL, Confidence TEXT, RS_vs_Nifty REAL, RS_vs_Sector REAL, Recovery REAL,
            Closing REAL, VWAP_Score REAL, Last_Hour_Vol REAL, Breakout_Score REAL, Reason TEXT
        )
        """)

        # 2026-08-25: from the "check-sheet" trade-setup qualification checklist
        # (check_sheet_logger.py, integrated from a reviewed Antigravity worktree
        # build) - a separate audit trail from `signals`, one row per manually
        # evaluated ticker, not per scan-produced candidate.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS check_sheet_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            symbol TEXT,
            strategy TEXT,
            total_score REAL,
            max_score REAL,
            percentage REAL,
            verdict TEXT,
            veto_count INTEGER,
            macro_score REAL,
            rs_score REAL,
            technical_score REAL,
            volume_score REAL,
            breakout_score REAL,
            risk_score REAL,
            details_json TEXT
        )
        """)
        self.conn.commit()

        # CREATE TABLE IF NOT EXISTS is a no-op against an existing signals.db created by an
        # older schema version, so any column added here since must be migrated in explicitly.
        expected_columns = {
            'timestamp': 'DATETIME', 'symbol': 'TEXT', 'strategy': 'TEXT',
            'score': 'REAL', 'rs_pctl': 'REAL', 'sector_rs': 'REAL', 'adx': 'REAL',
            'vol_ratio': 'REAL', 'entry': 'REAL', 'stop': 'REAL', 'target': 'REAL',
            'EMFB_Score': 'REAL', 'Confidence': 'TEXT', 'RS_vs_Nifty': 'REAL',
            'RS_vs_Sector': 'REAL', 'Recovery': 'REAL', 'Closing': 'REAL',
            'VWAP_Score': 'REAL', 'Last_Hour_Vol': 'REAL', 'Breakout_Score': 'REAL', 'Reason': 'TEXT'
        }
        existing_columns = self._get_existing_columns('signals')
        for col, dtype in expected_columns.items():
            if col not in existing_columns:
                cursor.execute(f'ALTER TABLE signals ADD COLUMN "{col}" {dtype}')
        self.conn.commit()

    def log_signal(self, s: dict):
        """Logs a signal dictionary to the database, dynamically handling columns."""
        try:
            cursor = self.conn.cursor()
            
            # Determine strategy from multiple possible keys
            strategy = str(s.get('strategy', s.get('Horizon', 'SWING'))).upper()
            
            # For historical generation, the timestamp is passed in. Otherwise, use now.
            timestamp = s.get('timestamp', datetime.now(config.MARKET_TZ).isoformat(timespec='seconds'))

            # Prepare data for insertion, using None for missing keys
            data_to_insert = {
                'timestamp': timestamp,
                'symbol': s.get('Symbol'),
                'strategy': strategy,
                'score': float(s['Score']) if s.get('Score') is not None else None,
                'rs_pctl': float(s['RS_Pctl']) if s.get('RS_Pctl') is not None else None,
                'sector_rs': float(s['Sector_RS']) if s.get('Sector_RS') is not None else None,
                'adx': float(s['ADX']) if s.get('ADX') is not None else None,
                'vol_ratio': float(s['Vol_Ratio']) if s.get('Vol_Ratio') is not None else None,
                'entry': float(s.get('Trigger', s.get('entry'))) if s.get('Trigger') is not None or s.get('entry') is not None else None,
                'stop': float(s.get('Stop', s.get('stop'))) if s.get('Stop') is not None or s.get('stop') is not None else None,
                'target': float(s.get('Target', s.get('target'))) if s.get('Target') is not None or s.get('target') is not None else None,
                'EMFB_Score': float(s['EMFB_Score']) if s.get('EMFB_Score') is not None else None,
                'Confidence': s.get('Confidence'),
                'RS_vs_Nifty': float(s['RS_vs_Nifty']) if s.get('RS_vs_Nifty') is not None else None,
                'RS_vs_Sector': float(s['RS_vs_Sector']) if s.get('RS_vs_Sector') is not None else None,
                'Recovery': float(s['Recovery']) if s.get('Recovery') is not None else None,
                'Closing': float(s['Closing']) if s.get('Closing') is not None else None,
                'VWAP_Score': float(s['VWAP_Score']) if s.get('VWAP_Score') is not None else None,
                'Last_Hour_Vol': float(s['Last_Hour_Vol']) if s.get('Last_Hour_Vol') is not None else None,
                'Breakout_Score': float(s['Breakout_Score']) if s.get('Breakout_Score') is not None else None,
                'Reason': s.get('Reason')
            }

            columns = ', '.join(data_to_insert.keys())
            placeholders = ', '.join('?' * len(data_to_insert))
            sql = f"INSERT INTO signals ({columns}) VALUES ({placeholders})"
            
            cursor.execute(sql, tuple(data_to_insert.values()))
            self.conn.commit()
        except (sqlite3.ProgrammingError, sqlite3.OperationalError) as e:
            if "closed" in str(e).lower():
                logger.warning("Database already closed, could not log signal.")
            else:
                logger.error(f"Database error logging signal for {s.get('Symbol')}: {e}", exc_info=True)

    def log_dataframe(self, df: pd.DataFrame, table_name: str):
        """Logs an entire DataFrame to a specified table, creating/altering as needed."""
        if df.empty:
            return

        # Sanitize column names for SQL
        df.columns = df.columns.str.replace(' ', '_').str.replace('%', 'Pct')

        # SQLite column names are case-insensitive, so e.g. 'Stop' and 'stop'
        # (EMFB's intentional lowercase aliases for backtester compatibility -
        # see scanner_engine.py) collide at CREATE TABLE time even though
        # pandas treats them as distinct columns. Keep the first occurrence of
        # each case-insensitive name and drop the rest before touching SQL -
        # the full data (both cases) still lands in the CSV/JSON/parquet
        # reports, which don't have this collision.
        df = df.loc[:, ~df.columns.str.lower().duplicated()]

        # Ensure table and columns exist
        existing_cols = self._get_existing_columns(table_name)
        if not existing_cols:
             # Create table if it doesn't exist
            df.to_sql(table_name, self.conn, if_exists='fail', index=False)
        else:
            # Add missing columns
            cursor = self.conn.cursor()
            for col in df.columns:
                if col not in existing_cols:
                    # This is a simplified type mapping
                    dtype = 'REAL' if pd.api.types.is_numeric_dtype(df[col]) else 'TEXT'
                    cursor.execute(f'ALTER TABLE {table_name} ADD COLUMN "{col}" {dtype}')
            self.conn.commit()

        # Append data
        df.to_sql(table_name, self.conn, if_exists='append', index=False)

    def log_check_sheet(self, cs) -> None:
        """Logs a CheckSheet object (or its .to_dict()) to check_sheet_logs."""
        try:
            cursor = self.conn.cursor()
            if hasattr(cs, 'to_dict'):
                data = cs.to_dict()
            elif isinstance(cs, dict):
                data = cs
            else:
                return

            pillar_scores = data.get('pillar_scores', {})
            sql = """
            INSERT INTO check_sheet_logs (
                timestamp, symbol, strategy, total_score, max_score, percentage,
                verdict, veto_count, macro_score, rs_score, technical_score,
                volume_score, breakout_score, risk_score, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            cursor.execute(sql, (
                data.get('timestamp', datetime.now(config.MARKET_TZ).isoformat(timespec='seconds')),
                data.get('symbol'),
                data.get('strategy', 'SWING'),
                float(data.get('total_score', 0.0)),
                float(data.get('max_possible_score', 100.0)),
                float(data.get('percentage', 0.0)),
                str(data.get('verdict', '')),
                len(data.get('vetoes', [])),
                float(pillar_scores.get('Market & Macro Regime', 0.0)),
                float(pillar_scores.get('Relative Strength & Sector Leadership', 0.0)),
                float(pillar_scores.get('Technical Trend & Momentum Structure', 0.0)),
                float(pillar_scores.get('Volume & Institutional Flow', 0.0)),
                float(pillar_scores.get('Price Action & Breakout Setup', 0.0)),
                float(pillar_scores.get('Risk-to-Reward & Trade Safety', 0.0)),
                json.dumps(data) if isinstance(data, dict) else str(data)
            ))
            self.conn.commit()
        except (sqlite3.ProgrammingError, sqlite3.OperationalError) as e:
            if "closed" in str(e).lower():
                logger.warning("Database already closed, could not log check sheet.")
            else:
                logger.error(f"Database error logging check sheet: {e}", exc_info=True)

    def close(self):
        if self.conn:
            print("Closing database...")
            logger.info("Closing database connection.")
            self.conn.close()
            self.conn = None
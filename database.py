import sqlite3
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

    def close(self):
        if self.conn:
            print("Closing database...")
            logger.info("Closing database connection.")
            self.conn.close()
            self.conn = None
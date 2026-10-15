"""Score only signals that have no final outcome yet, then rebuild the tier scorecards.

`backtest.py` replays and UPSERTS every signal from cached 15-minute candles. That cache
only spans ~80 days, so a full rerun can overwrite finished May-July trades with
"no candles". This runner leaves terminal outcomes untouched.

    python score_new_outcomes.py            # score + save
    python score_new_outcomes.py --dry-run  # score, print, save nothing
"""
import argparse
import sqlite3
from collections import Counter

import pandas as pd

import backtest as bt


# utils.add_decision_scores (shared with the live scanner) reads TitleCase scanner
# columns; signals.db stores them lowercase, so every replay raised KeyError('Trigger').
_SCANNER_ALIASES = {"Trigger": "entry", "Stop": "stop", "Score": "score",
                    "RS_Pctl": "rs_pctl", "Sector_RS": "sector_rs", "ADX": "adx"}


def _with_scanner_columns(row: pd.Series) -> pd.Series:
    row = row.copy()
    for wanted, stored in _SCANNER_ALIASES.items():
        if wanted not in row and stored in row:
            row[wanted] = row[stored]
    return row


def pending_signals(conn: sqlite3.Connection) -> pd.DataFrame:
    signals = bt.load_signals(conn)
    done = pd.read_sql_query("SELECT signal_id, outcome FROM outcomes", conn)
    terminal_ids = set(done.loc[done["outcome"].isin(bt.TERMINAL_OUTCOMES), "signal_id"])
    id_col = "id" if "id" in signals.columns else "_rowid"
    return signals[~signals[id_col].isin(terminal_ids)]


def run(db_path: str = "signals.db", cache_dir: str = "historical_data", dry_run: bool = False) -> Counter:
    conn = sqlite3.connect(db_path)
    try:
        bt.ensure_signals_schema(conn)
        bt.ensure_outcomes_schema(conn)
        todo = pending_signals(conn)
        store = bt.CandleStore(cache_dir=cache_dir)
        results = []
        for _, row in todo.iterrows():
            try:
                results.append(bt.evaluate_signal(_with_scanner_columns(row), store))
            except Exception as exc:
                skipped = bt.skipped_result(row, f"ERROR_{type(exc).__name__}", store)
                skipped.exit_reason = str(exc)[:250]
                results.append(skipped)
        if not dry_run:
            bt.save_outcomes(conn, results)
    finally:
        conn.close()
    counts = Counter(r.outcome for r in results)
    print(f"pending signals scored: {len(results)}{' (dry run, nothing saved)' if dry_run else ''}")
    for outcome, n in counts.most_common():
        print(f"  {outcome}: {n}")
    return counts


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="signals.db")
    p.add_argument("--cache-dir", default="historical_data")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    run(a.db, a.cache_dir, a.dry_run)

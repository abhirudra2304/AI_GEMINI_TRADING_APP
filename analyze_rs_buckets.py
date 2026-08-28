import sqlite3
import pandas as pd
import numpy as np
import os
import sys

# Add the project root to the Python path to resolve import issues
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- AUTO VENV ACTIVATION ---
from venv_activator import ensure_venv
ensure_venv()
# --------------------------

DB_PATH = "signals.db"

# RS buckets
bins = [0, 30, 50, 70, 85, 100]
labels = ['0-30', '30-50', '50-70', '70-85', '85-100']

def load_data(db_path):
    conn = sqlite3.connect(db_path)
    df = pd.read_sql("""
    SELECT s.id as signal_id,
           s.timestamp,
           s.symbol,
           s.rs_pctl,
           s.entry,
           s.stop,
           s.target,
           o.outcome,
           o.return,
           -- optional columns that some workflows may store
           o.mfe, o.mae
    FROM signals s
    LEFT JOIN outcomes o ON s.id = o.signal_id
    """, conn, parse_dates=['timestamp'])
    conn.close()
    return df

def compute_stats(df):
    df = df.copy()
    # Keep only rows with outcome or return
    df = df[df['outcome'].notna() | df['return'].notna()].copy()
    if df.empty:
        print("No labeled signals found in DB.")
        return

    # target & numeric return
    df['ret'] = df['return']
    df['success'] = np.where(df['ret'].notna(), df['ret'] > 0, df['outcome'].astype(str).str.upper() == 'WIN')

    # RS bucket
    df['rs_bucket'] = pd.cut(df['rs_pctl'].fillna(-999), bins=bins, labels=labels, include_lowest=True, right=True)

    # detect presence of mfe/mae columns
    mfe_present = 'mfe' in df.columns and df['mfe'].notna().any()
    mae_present = 'mae' in df.columns and df['mae'].notna().any()

    rows = []
    for lb in labels:
        sub = df[df['rs_bucket'] == lb]
        cnt = len(sub)
        if cnt == 0:
            rows.append((lb, 0, np.nan, np.nan, np.nan, np.nan))
            continue
        win_rate = sub['success'].mean()
        avg_return = sub['ret'].mean()
        # avg MFE / MAE if present
        avg_mfe = sub['mfe'].mean() if mfe_present else np.nan
        avg_mae = sub['mae'].mean() if mae_present else np.nan
        rows.append((lb, cnt, win_rate, avg_return, avg_mfe, avg_mae))

    out = pd.DataFrame(rows, columns=['rs_bucket','count','win_rate','avg_return','avg_mfe','avg_mae'])
    pd.set_option('display.float_format', lambda x: f"{x:.4f}" if pd.notna(x) else "nan")
    print("\nRS Percentile buckets (0-30,30-50,50-70,70-85,85-100)\n")
    print(out.to_string(index=False))

    # Summary observation
    print("\nNotes:")
    print("- 'avg_mfe' and 'avg_mae' shown only if stored in outcomes table (columns: mfe, mae).")
    print("- If mfe/mae are not present, consider recording MFE/MAE at outcome logging or compute from price history with hold timestamps.")

    # Minimal interpretation heuristics
    high_bucket = out.loc[out['rs_bucket'] == '85-100']
    low_bucket = out.loc[out['rs_bucket'] == '0-30']
    if not high_bucket.empty and not low_bucket.empty and pd.notna(high_bucket['win_rate'].iloc[0]) and pd.notna(low_bucket['win_rate'].iloc[0]):
        if high_bucket['win_rate'].iloc[0] < low_bucket['win_rate'].iloc[0]:
            print("\nInterpretation: High RS (85-100) shows LOWER win rate than low RS (0-30). This pattern is consistent with entries after extended moves (mean-reversion risk).")
        else:
            print("\nInterpretation: High RS bucket performs at least as well as low RS in this dataset.")
    else:
        print("\nInterpretation: Insufficient data to compare high vs low RS buckets reliably.")

if __name__ == '__main__':
    df = load_data(DB_PATH)
    compute_stats(df)
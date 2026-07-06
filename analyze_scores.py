# Run: python analyze_scores.py
import sqlite3
import pandas as pd
import numpy as np
from scipy import stats

DB = "signals.db"
score_cols_preferred = ["Decision_Score", "Decision_Score".lower(), "score", "Score"]

buckets = [(0,20),(20,40),(40,60),(60,80),(80,100)]
labels = ["0-20","20-40","40-60","60-80","80-100"]

def profit_factor(returns):
    pos = returns[returns>0].sum()
    neg = returns[returns<0].sum()
    if pd.isna(pos) and pd.isna(neg): return np.nan
    if neg == 0:
        return np.inf if pos>0 else np.nan
    return pos / abs(neg)

def analyze_column(df, col):
    df = df.copy()
    df['score_val'] = pd.to_numeric(df[col], errors='coerce')
    df = df[df['score_val'].notna()]
    if df.empty:
        return None

    # target
    df['ret'] = df['return']
    df['success'] = np.where(df['ret'].notna(), df['ret']>0, df['outcome'].astype(str).str.upper()=='WIN')

    rows = []
    for lo, hi in buckets:
        lab = f"{lo}-{hi}"
        sub = df[(df['score_val'] >= lo) & (df['score_val'] < hi)]
        cnt = len(sub)
        if cnt == 0:
            rows.append((lab,0,np.nan,np.nan,np.nan))
            continue
        win_rate = sub['success'].mean()
        avg_return = sub['ret'].mean()
        pf = profit_factor(sub['ret'].dropna()) if sub['ret'].notna().any() else np.nan
        rows.append((lab,cnt,win_rate,avg_return,pf))

    table = pd.DataFrame(rows, columns=['bucket','count','win_rate','avg_return','profit_factor'])
    overall_win = df['success'].mean()
    overall_count = len(df)

    # correlation tests
    try:
        rho, pval = stats.spearmanr(df['score_val'], df['success'].astype(int), nan_policy='omit')
    except Exception:
        rho, pval = np.nan, np.nan

    try:
        rho_r, pval_r = stats.spearmanr(df['score_val'], df['ret'], nan_policy='omit')
    except Exception:
        rho_r, pval_r = np.nan, np.nan

    # Recommendation logic (simple)
    recommended_thresholds = []
    # find lowest threshold (bucket boundary) with win_rate > overall_win and pf>1
    for lo,hi in buckets[::-1]:
        lab = f"{lo}-{hi}"
        row = table[table['bucket']==lab]
        if not row.empty and row['count'].iloc[0]>0:
            if (row['win_rate'].iloc[0] > overall_win) and (row['profit_factor'].iloc[0] > 1):
                recommended_thresholds.append(lo)
                break

    if recommended_thresholds:
        thr = recommended_thresholds[0]
        remaining = df[df['score_val'] >= thr]
        reduction = 100.0 * (1 - len(remaining)/overall_count)
        new_win = remaining['success'].mean()
        win_improve = 100.0 * (new_win - overall_win)
    else:
        thr = None
        reduction = 0.0
        new_win = overall_win
        win_improve = 0.0

    return {
        "col": col,
        "overall_count": overall_count,
        "overall_win": overall_win,
        "spearman_success_rho": rho,
        "spearman_success_p": pval,
        "spearman_return_rho": rho_r,
        "spearman_return_p": pval_r,
        "table": table,
        "recommended_threshold": thr,
        "estimated_signal_reduction_pct": reduction,
        "estimated_new_win_rate": new_win,
        "estimated_win_rate_improvement_pct": win_improve
    }

def main():
    conn = sqlite3.connect(DB)
    df = pd.read_sql("""
    SELECT s.*, o.outcome, o.return
    FROM signals s
    LEFT JOIN outcomes o ON s.id = o.signal_id
    """, conn, parse_dates=['timestamp'])
    conn.close()

    # find available score column
    col_found = None
    for c in score_cols_preferred:
        if c in df.columns:
            col_found = c
            break
    if col_found is None:
        print("No score column found among:", score_cols_preferred)
        return

    res = analyze_column(df, col_found)
    if res is None:
        print(f"No data for column {col_found}")
        return

    print(f"\nAnalyzing score column: {col_found}")
    print(f"Overall signals: {res['overall_count']:,}  |  Overall win rate: {res['overall_win']:.3f}")
    print(f"Spearman(success vs score): rho={res['spearman_success_rho']:.3f}, p={res['spearman_success_p']:.3f}")
    print(f"Spearman(return vs score): rho={res['spearman_return_rho']:.3f}, p={res['spearman_return_p']:.3f}")
    print("\nBuckets:")
    print(res['table'].to_string(index=False))
    if res['recommended_threshold'] is not None:
        print(f"\nRecommended threshold: score >= {res['recommended_threshold']}")
        print(f"Estimated signal reduction: {res['estimated_signal_reduction_pct']:.1f}%")
        print(f"Estimated new win rate: {res['estimated_new_win_rate']:.3f} (improvement {res['estimated_win_rate_improvement_pct']:.2f}%)")
    else:
        print("\nNo simple threshold found that improves win rate + profit factor based on available returns.")

if __name__ == '__main__':
    main()
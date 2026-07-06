import pandas as pd
import numpy as np
import sqlite3
import warnings
warnings.filterwarnings('ignore')

def calculate_metrics(df):
    """Calculates PF, Win Rate, and Expectancy."""
    if df.empty:
        return pd.Series({'N': 0, 'Win_Rate': 0.0, 'PF': 0.0, 'Expectancy': 0.0})
    
    wins = df[df['adj_return_decimal'] > 0]
    losses = df[df['adj_return_decimal'] <= 0]
    
    n_trades = len(df)
    win_rate = len(wins) / n_trades
    
    gross_profit = wins['adj_return_decimal'].sum()
    gross_loss = abs(losses['adj_return_decimal'].sum())
    
    pf = gross_profit / gross_loss if gross_loss > 0 else (np.inf if gross_profit > 0 else 0)
    
    avg_win = wins['adj_return_decimal'].mean() if not wins.empty else 0
    avg_loss = abs(losses['adj_return_decimal'].mean()) if not losses.empty else 0
    
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    
    return pd.Series({'N': n_trades, 'Win_Rate': win_rate, 'PF': pf, 'Expectancy': expectancy})

def analyze_buckets(df, score_column):
    """Calculates metrics for Top 10%, 20%, 30%, and Bottom 30%."""
    if df.empty or score_column not in df.columns or df[score_column].isna().all():
        return None
    
    # Calculate quantile thresholds
    top_10_thresh = df[score_column].quantile(0.90)
    top_20_thresh = df[score_column].quantile(0.80)
    top_30_thresh = df[score_column].quantile(0.70)
    bot_30_thresh = df[score_column].quantile(0.30)
    
    buckets = {
        'Top 10%': df[df[score_column] >= top_10_thresh],
        'Top 20%': df[df[score_column] >= top_20_thresh],
        'Top 30%': df[df[score_column] >= top_30_thresh],
        'Bottom 30%': df[df[score_column] <= bot_30_thresh]
    }
    
    results = []
    for name, bucket_df in buckets.items():
        metrics = calculate_metrics(bucket_df)
        metrics['Bucket'] = name
        results.append(metrics)
        
    return pd.DataFrame(results).set_index('Bucket')

def evaluate_stability(year_metrics):
    """Evaluates edge stability based on consistency across years."""
    pfs = [year_metrics[year]['PF'] for year in year_metrics if year_metrics[year]['N'] > 10]
    
    if not pfs:
        return "Insufficient data for stability check"
        
    min_pf = min(pfs)
    max_pf = max(pfs)
    pf_variance = np.var(pfs)
    
    if min_pf > 1.2 and pf_variance < 0.2:
        return "Stable edge (Consistent PF > 1.2 across periods)"
    elif min_pf < 1.0 and max_pf > 1.5:
        return "Unstable edge (High variance, regime-dependent)"
    elif pfs[-1] < 1.0 and pfs[0] > 1.5: # 2026 poor, 2024 great
        return "Possible overfitting signs (Degradation in OOS/Recent data)"
    else:
        return "Marginal edge (Needs larger sample)"

def main():
    print("Loading backtest results and signal database...")
    
    # 1. Load Data
    try:
        bt_df = pd.read_csv("backtest_results.csv")
        bt_df['entry_time'] = pd.to_datetime(bt_df['entry_time'])
        bt_df['Year'] = bt_df['entry_time'].dt.year
        
        # Connect to DB to fetch base scores and RS if missing from CSV
        conn = sqlite3.connect('signals.db')
        sig_df = pd.read_sql_query("SELECT symbol, timestamp as signal_timestamp, score as original_score, rs_pctl FROM signals", conn)
        conn.close()
        
        # Format timestamps for merge
        # Format timestamps for merge
        sig_df['signal_timestamp'] = pd.to_datetime(
            sig_df['signal_timestamp'],
            format='mixed',
            utc=True,
            errors='coerce'
        )

        bt_df['signal_timestamp'] = pd.to_datetime(
            bt_df['signal_timestamp'],
            format='mixed',
            utc=True,
            errors='coerce'
        )

        df = pd.merge(
            bt_df,
            sig_df,
            on=['symbol', 'signal_timestamp'],
            how='left'
        )

        df['Original_Score'] = df['original_score']
        df['New_Decision_Score'] = df['decision_score']            
    except Exception as e:
        print(f"Data loading error: {e}")
        return

    years = [2024, 2025, 2026]
    score_types = ['Original_Score', 'Original_Decision_Score', 'New_Decision_Score']
    
    print("\n" + "="*80)
    print(" 1. YEARLY METRIC COMPARISON")
    print("="*80)
    
    stability_reports = {}
    
    for score_type in score_types:
        if score_type not in df.columns:
            continue
            
        print(f"\n--- Metric: {score_type} ---")
        year_metrics = {}
        
        for year in years:
            period_df = df[df['Year'] == year]
            if period_df.empty:
                continue
                
            metrics = calculate_metrics(period_df)
            year_metrics[year] = metrics
            
            print(f"[{year}] N: {int(metrics['N']):<4} | Win Rate: {metrics['Win_Rate']:.2%} | PF: {metrics['PF']:.2f} | Exp: {metrics['Expectancy']:.4f}")
            
        stability_reports[score_type] = evaluate_stability(year_metrics)

    print("\n" + "="*80)
    print(" 2. QUANTILE BUCKET TESTING (All Years combined)")
    print("="*80)
    
    for score_type in score_types:
        if score_type not in df.columns:
            continue
            
        print(f"\n--- Bucket Test: {score_type} ---")
        bucket_results = analyze_buckets(df, score_type)
        if bucket_results is not None:
            print(bucket_results[['N', 'Win_Rate', 'PF', 'Expectancy']].to_string(float_format=lambda x: f"{x:.4f}"))

    print("\n" + "="*80)
    print(" 3. INVERSE PREDICTIVE POWER (RS & Score)")
    print("="*80)
    
    inverse_metrics = ['Original_Score', 'rs_pctl']
    for im in inverse_metrics:
        if im in df.columns:
            buckets = analyze_buckets(df, im)
            if buckets is not None:
                top_10_pf = buckets.loc['Top 10%', 'PF']
                bot_30_pf = buckets.loc['Bottom 30%', 'PF']
                
                print(f"\nTesting {im}:")
                print(f"Top 10% PF:    {top_10_pf:.2f}")
                print(f"Bottom 30% PF: {bot_30_pf:.2f}")
                
                if bot_30_pf > top_10_pf and bot_30_pf > 1.0:
                    print(f"⚠️ INVERSE EDGE DETECTED: Low {im} strongly outperforms high {im}. Strategy is functioning as a mean-reversion system, contradicting momentum logic.")
                elif bot_30_pf > 1.0:
                    print(f"⚠️ POOR SEPARATION: Bottom 30% remains profitable. {im} fails to filter out bad trades effectively.")
                else:
                    print(f"✅ LOGIC HOLDING: Bottom 30% is unprofitable/inferior. {im} correctly filters poor setups.")

    print("\n" + "="*80)
    print(" 4. FINAL STABILITY VERIFICATION REPORT")
    print("="*80)
    for score_type, report in stability_reports.items():
        print(f"{score_type}: {report}")

if __name__ == "__main__":
    main()
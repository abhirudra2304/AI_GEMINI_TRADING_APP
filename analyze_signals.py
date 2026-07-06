# python
import sqlite3
import pandas as pd
import numpy as np

conn = sqlite3.connect("signals.db")
df = pd.read_sql("""
SELECT s.id as signal_id, s.timestamp, s.symbol, s.score, s.rs_pctl, s.sector_rs, s.adx, s.vol_ratio,
       o.outcome, o.return
FROM signals s
LEFT JOIN outcomes o ON s.id = o.signal_id
""", conn, parse_dates=['timestamp'])
conn.close()

# Keep only rows with outcome info
df = df[df['outcome'].notna() | df['return'].notna()].copy()

# Normalize target: prefer numeric return when present
df['ret'] = df['return']
df['success'] = np.where(df['ret'].notna(), df['ret'] > 0, df['outcome'].str.upper() == 'WIN')

def stats_table(series, bins, labels):
    df['bin'] = pd.cut(series.fillna(-999), bins=bins, labels=labels, include_lowest=True)
    out = []
    for lab in labels:
        sub = df[df['bin'] == lab]
        if sub.empty:
            out.append((lab, 0, np.nan, np.nan, np.nan, np.nan))
            continue
        returns = sub['ret'].dropna()
        wins = sub['success'].mean()
        avg_return = returns.mean() if not returns.empty else np.nan
        avg_gain = returns[returns>0].mean() if not returns[returns>0].empty else np.nan
        avg_loss = returns[returns<0].mean() if not returns[returns<0].empty else np.nan
        profit_factor = (returns[returns>0].sum() / abs(returns[returns<0].sum())) if returns[returns<0].sum() != 0 else np.nan
        out.append((lab, len(sub), wins, avg_gain, avg_loss, profit_factor))
    return pd.DataFrame(out, columns=['bucket','count','win_rate','avg_gain','avg_loss','profit_factor'])

# RS Percentile
rs_bins = [-0.1,50,70,85,100]
rs_labels = ['<50','50-70','70-85','>85']
table_rs = stats_table(df['rs_pctl'], rs_bins, rs_labels)

# ADX
adx_bins = [-0.1,20,25,35,999]
adx_labels = ['<20','20-25','25-35','>35']
table_adx = stats_table(df['adx'], adx_bins, adx_labels)

# Sector RS
sec_bins = [-0.1,50,70,100]
sec_labels = ['<50','50-70','>70']
table_sector = stats_table(df['sector_rs'], sec_bins, sec_labels)

# Volume Ratio
vol_bins = [-0.1,1,1.5,2,999]
vol_labels = ['<1','1-1.5','1.5-2','>2']
table_vol = stats_table(df['vol_ratio'], vol_bins, vol_labels)

pd.set_option('display.float_format', lambda x: f"{x:.4f}" if pd.notna(x) else "nan")
print("\nRS Percentile")
print(table_rs.to_string(index=False))
print("\nADX")
print(table_adx.to_string(index=False))
print("\nSector RS")
print(table_sector.to_string(index=False))
print("\nVolume Ratio")
print(table_vol.to_string(index=False))
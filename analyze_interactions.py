import sqlite3
import pandas as pd
import numpy as np

conn = sqlite3.connect("signals.db")

df = pd.read_sql("""
SELECT s.id as signal_id,
       s.timestamp,
       s.symbol,
       s.score,
       s.rs_pctl,
       s.sector_rs,
       s.adx,
       s.vol_ratio,
       o.outcome,
       o.return
FROM signals s
LEFT JOIN outcomes o
ON s.id = o.signal_id
""", conn)

conn.close()

# Keep rows with outcomes
df = df[df['outcome'].notna() | df['return'].notna()].copy()

df['ret'] = df['return']

df['success'] = np.where(
    df['ret'].notna(),
    df['ret'] > 0,
    df['outcome'].astype(str).str.upper() == 'WIN'
)

# Buckets
rs_bins = [-0.1, 50, 70, 85, 100]
rs_labels = ['<50', '50-70', '70-85', '>85']

adx_bins = [-0.1, 20, 25, 35, 999]
adx_labels = ['<20', '20-25', '25-35', '>35']

sec_bins = [-0.1, 50, 70, 100]
sec_labels = ['<50', '50-70', '>70']

vol_bins = [-0.1, 1, 1.5, 2, 999]
vol_labels = ['<1', '1-1.5', '1.5-2', '>2']

df['rs_bucket'] = pd.cut(df['rs_pctl'], rs_bins, labels=rs_labels)
df['adx_bucket'] = pd.cut(df['adx'], adx_bins, labels=adx_labels)
df['sec_bucket'] = pd.cut(df['sector_rs'], sec_bins, labels=sec_labels)
df['vol_bucket'] = pd.cut(df['vol_ratio'], vol_bins, labels=vol_labels)

def calc_stats(group):
    count = len(group)

    win_rate = group['success'].mean()

    avg_return = group['ret'].mean()

    pos = group.loc[group['ret'] > 0, 'ret']
    neg = group.loc[group['ret'] < 0, 'ret']

    profit_factor = (
        pos.sum() / abs(neg.sum())
        if len(neg) > 0 and abs(neg.sum()) > 0
        else np.nan
    )

    return pd.Series({
        'count': count,
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'avg_return': avg_return
    })

# RS × Volume
rs_vol = (
    df.groupby(['rs_bucket', 'vol_bucket'])
      .apply(calc_stats)
      .reset_index()
)

# RS × ADX
rs_adx = (
    df.groupby(['rs_bucket', 'adx_bucket'])
      .apply(calc_stats)
      .reset_index()
)

# Sector × Volume
sec_vol = (
    df.groupby(['sec_bucket', 'vol_bucket'])
      .apply(calc_stats)
      .reset_index()
)

# ADX × Volume
adx_vol = (
    df.groupby(['adx_bucket', 'vol_bucket'])
      .apply(calc_stats)
      .reset_index()
)

def make_rank(df_group, cols, matrix):
    t = df_group.copy()

    t['combination'] = (
        t[cols].astype(str).agg(' | '.join, axis=1)
    )

    t['matrix'] = matrix

    return t[
        [
            'matrix',
            'combination',
            'count',
            'win_rate',
            'profit_factor',
            'avg_return'
        ]
    ]

all_rank = pd.concat([
    make_rank(rs_vol, ['rs_bucket', 'vol_bucket'], 'RSxVol'),
    make_rank(rs_adx, ['rs_bucket', 'adx_bucket'], 'RSxADX'),
    make_rank(sec_vol, ['sec_bucket', 'vol_bucket'], 'SecxVol'),
    make_rank(adx_vol, ['adx_bucket', 'vol_bucket'], 'ADXxVol')
])

# Filter low-sample noise
ranked = (
    all_rank[all_rank['count'] >= 30]
    .sort_values(
        'profit_factor',
        ascending=False
    )
    .head(20)
)

pd.set_option(
    'display.float_format',
    lambda x: f"{x:.4f}"
)

print("\n===== RS x Volume =====")
print(rs_vol.to_string(index=False))

print("\n===== RS x ADX =====")
print(rs_adx.to_string(index=False))

print("\n===== Sector RS x Volume =====")
print(sec_vol.to_string(index=False))

print("\n===== ADX x Volume =====")
print(adx_vol.to_string(index=False))

print("\n===== TOP 20 COMBINATIONS =====")
print(ranked.to_string(index=False))

ranked.to_csv(
    "interaction_stats_top20.csv",
    index=False
)

print("\nSaved: interaction_stats_top20.csv")
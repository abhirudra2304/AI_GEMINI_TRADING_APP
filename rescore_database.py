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

DB = "signals.db"

conn = sqlite3.connect(DB)

df = pd.read_sql("""
SELECT id, score, rs_pctl, sector_rs, adx, vol_ratio
FROM signals
""", conn)

# ---- New Scoring Model ----

def calculate_score(row):

    rs = row["rs_pctl"]
    sector = row["sector_rs"]
    adx = row["adx"]
    vol = row["vol_ratio"]

    # Volume Score
    vol_center = 1.25
    vol_sigma = 0.5

    volume_score = 30.0 * np.exp(
        -0.5 * (((vol - vol_center) / vol_sigma) ** 2)
    )

    overshoot_penalty = 0

    if vol > 5:
        overshoot_penalty = 15
    elif vol > 3:
        overshoot_penalty = 8

    # ADX Score

    if adx < 5 or adx > 40:
        adx_score = 0

    elif 20 <= adx <= 25:
        adx_score = 25

    elif adx < 20:
        adx_score = 25 * ((adx - 5) / 15)

    else:
        adx_score = 25 * ((40 - adx) / 15)

    adx_score = max(0, adx_score)

    # RS Score

    if rs <= 70:
        rs_score = 20 * (rs / 70)

    elif rs <= 85:
        rs_score = 20 * ((85 - rs) / 15)

    else:
        rs_score = -10 * ((rs - 85) / 15)

    rs_score = max(-10, rs_score)

    # Sector Score

    if sector < 40:
        sector_score = 0

    elif sector <= 70:
        sector_score = 7 * ((sector - 40) / 30)

    elif sector <= 85:
        sector_score = (7 * ((85 - sector) / 15)) - 5

    else:
        sector_score = -15

    sector_score = max(-15, sector_score)

    score = (
        volume_score +
        adx_score +
        rs_score +
        sector_score -
        overshoot_penalty
    )

    return round(max(0, min(100, score)), 1)

df["new_score"] = df.apply(calculate_score, axis=1)

# Add column if missing

try:
    conn.execute(
        "ALTER TABLE signals ADD COLUMN new_score REAL"
    )
except:
    pass

# Update database

for _, row in df.iterrows():

    conn.execute(
        """
        UPDATE signals
        SET new_score = ?
        WHERE id = ?
        """,
        (float(row["new_score"]), int(row["id"]))
    )

conn.commit()

print("\nRescoring complete.\n")

print(df[["score","new_score"]].describe())

conn.close()
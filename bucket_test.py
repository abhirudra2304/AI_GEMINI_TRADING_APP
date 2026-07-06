import sqlite3
import pandas as pd

conn = sqlite3.connect("signals.db")

df = pd.read_sql("""
SELECT s.new_score, o.return
FROM signals s
JOIN outcomes o
ON s.id = o.signal_id
WHERE s.new_score IS NOT NULL
""", conn)

conn.close()

df["win"] = df["return"] > 0

print("\nNEW SCORE VALIDATION\n")

for lo, hi in [(0,20),(20,40),(40,60),(60,80)]:

    sub = df[
        (df["new_score"] >= lo) &
        (df["new_score"] < hi)
    ]

    wr = sub["win"].mean()
    avg_ret = sub["return"].mean()

    print(
        f"{lo}-{hi}: "
        f"n={len(sub)}, "
        f"win_rate={wr:.3f}, "
        f"avg_return={avg_ret:.4f}"
    )
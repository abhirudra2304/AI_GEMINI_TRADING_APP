"""Run the real BTST/SWING engine at decision time and save its picks for the brief.

EOD persists the engine's signals to signals.db only after the closing auction (15:35),
so the 14:30-15:15 brief always fell back to fitness tags. This runs the same
`run_fast_execution_scan` path EOD uses, with persist=False so signals.db keeps exactly
one (settled) row per signal, and writes engine_snapshot_<YYYYMMDD>_<HHMMSS>.csv.

The engine waits out the 15:15-15:35 auction for F&O names, so this refuses to start
after LATEST_START.
"""
import sys
from datetime import datetime, time

import pandas as pd

import config
from log_redaction import install_redaction

install_redaction()

LATEST_START = time(15, 11)
COLUMNS = ["Symbol", "strategy", "Engine_Score", "Trigger", "Stop", "Target"]


def snapshot(now: datetime = None) -> str:
    now = now or datetime.now(config.MARKET_TZ)
    if now.time() >= LATEST_START:
        print(f"engine snapshot skipped: {now:%H:%M} is past {LATEST_START:%H:%M} (closing-auction wait would stall the brief)")
        return ""

    from data_broker import DataBroker
    from orchestrator import run_fast_execution_scan
    from scanner_engine import HybridScanner

    broker = DataBroker()
    scanner = HybridScanner(base_multiplier=2.0, alpha=0.5, max_risk_per_trade=5000, max_capital_per_trade=100000)
    frames = []
    for strategy in ("BTST", "SWING"):
        try:
            df = run_fast_execution_scan(broker, scanner, strategy=strategy, top_n=config.Discovery.TOP_N_FAST_SCAN,
                                         display=False, caller="DecisionSnapshot", persist=False)
        except Exception as e:
            print(f"{strategy} engine failed: {e}")
            continue
        if df is None or df.empty:
            print(f"{strategy}: engine returned no signals")
            continue
        out = pd.DataFrame({
            "Symbol": df["Symbol"],
            "strategy": strategy,
            "Engine_Score": pd.to_numeric(df.get("Score"), errors="coerce"),
            "Trigger": df["Trigger"], "Stop": df["Stop"], "Target": df["Target"],
        })
        print(f"{strategy}: {len(out)} engine signals")
        frames.append(out)

    path = f"engine_snapshot_{now:%Y%m%d_%H%M%S}.csv"
    (pd.concat(frames) if frames else pd.DataFrame(columns=COLUMNS)).to_csv(path, index=False)
    print(f"saved {path}")
    return path


if __name__ == "__main__":
    sys.exit(0 if snapshot() is not None else 1)

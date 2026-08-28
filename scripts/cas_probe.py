"""
One-off diagnostic (NOT wired into any scheduled task): polls broker.fetch_ohlcv()
(force_refresh, bypassing the 50s cache) across the 15:15-15:35 Closing Auction
Session window to observe actual REST candle behavior. Read-only investigation
for the SEBI CAS fact-finding pass - does not touch scan logic.

NOTE: get_live_candles()'s websocket/live-tick path (_on_data in data_broker.py)
is currently an unimplemented stub (`pass`) and self.ws.is_connected() raises
AttributeError on SmartWebSocketV2 - so that path is not testable and, more
importantly, is not actually in use: get_live_candles() always falls through to
plain fetch_ohlcv() historical data today, CAS or not. This probe tests that
real path instead.
"""
import sys
import os
import time
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import venv_activator
venv_activator.ensure_venv()

from data_broker import DataBroker

SYMBOLS_FNO = ["RELIANCE", "TCS"]
SYMBOLS_NON_FNO = ["ACC", "TATACHEM"]
ALL_SYMBOLS = SYMBOLS_FNO + SYMBOLS_NON_FNO
INTERVAL = "FIVE_MINUTE"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "cas_probe.jsonl")

def log(entry):
    entry["logged_at"] = datetime.now().isoformat()
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(entry, flush=True)

def poll(broker):
    for sym in ALL_SYMBOLS:
        try:
            df = broker.fetch_ohlcv(sym, INTERVAL, days_back=2, force_refresh=True, caller="CAS_Probe")
            if df.empty:
                log({"symbol": sym, "status": "EMPTY"})
                continue
            tail = df.tail(3)
            log({
                "symbol": sym,
                "status": "OK",
                "rows": len(df),
                "last_3": [
                    {"ts": str(r["Timestamp"]), "close": float(r["Close"]), "vol": float(r["Volume"])}
                    for _, r in tail.iterrows()
                ],
            })
        except Exception as e:
            log({"symbol": sym, "status": "ERROR", "error": str(e)})

def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log({"event": "PROBE_START", "symbols": ALL_SYMBOLS, "interval": INTERVAL})

    broker = DataBroker()

    # No internal pre-window wait: this script is launched at the right time by
    # an OS-level Task Scheduler entry (see scripts/register_cas_probe_task.ps1),
    # not by a long-lived process inside this session (which was getting killed
    # by the session's own background-task lifetime limit before reaching 15:08).
    end_time = datetime.now().replace(hour=15, minute=42, second=0, microsecond=0)
    while datetime.now() < end_time:
        log({"event": "POLL", "clock": datetime.now().strftime("%H:%M:%S")})
        poll(broker)
        time.sleep(90)

    log({"event": "PROBE_END"})

if __name__ == "__main__":
    main()

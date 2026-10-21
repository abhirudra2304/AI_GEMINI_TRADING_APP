"""Is NSE's cash market open today? Uses NSE's official trading-holiday calendar.

The scheduled scans only know "Mon-Fri". On 2026-10-20 (Dussehra) Prewarm, the decision
scan and EOD all ran in full on the previous day's data and saved 19 BTST/SWING signals,
16 of them copies of the day before's.

    python market_calendar.py            # exit 0 = trading day, 3 = NSE holiday
    python market_calendar.py 2026-11-10

The calendar is cached in nse_holidays.json (refreshed weekly). If NSE can't be reached
and there is no usable cache, a weekday is assumed to be a trading day, so an outage never
blocks a real session.
"""
import json
import os
import sys
import time
from datetime import date, datetime
from typing import Optional

import requests

CACHE_PATH = "nse_holidays.json"
URL = "https://www.nseindia.com/api/holiday-master?type=trading"
MAX_AGE_DAYS = 7
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
}


def _fetch() -> Optional[dict]:
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get("https://www.nseindia.com", timeout=10)
        rows = s.get(URL, timeout=20).json().get("CM") or []
        holidays = {datetime.strptime(r["tradingDate"], "%d-%b-%Y").date().isoformat(): r.get("description", "")
                    for r in rows}
        return {"fetched_at": time.time(), "holidays": holidays} if holidays else None
    except Exception:
        return None


def _load(path: str = CACHE_PATH) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def holidays(path: str = CACHE_PATH, fetch=_fetch) -> dict:
    """{'YYYY-MM-DD': description}. Refreshes the cache when it is missing or stale."""
    cached = _load(path)
    fresh = cached and (time.time() - cached.get("fetched_at", 0)) < MAX_AGE_DAYS * 86400
    if not fresh:
        new = fetch()
        if new:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(new, f, indent=1)
            cached = new
    return (cached or {}).get("holidays", {})


def market_status(d: Optional[date] = None, path: str = CACHE_PATH, fetch=_fetch) -> tuple:
    """(is_trading_day, reason). Reason is empty on a trading day."""
    d = d or date.today()
    if d.weekday() >= 5:
        return False, "weekend"
    name = holidays(path, fetch).get(d.isoformat())
    return (False, f"NSE holiday: {name}") if name is not None else (True, "")


if __name__ == "__main__":
    when = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    ok, why = market_status(when)
    print("TRADING DAY" if ok else why)
    sys.exit(0 if ok else 3)

"""Append NSE's provisional FII/FPI and DII cash-market net flows to fii_dii_history.csv.

NSE's /api/fiidiiTradeReact only returns the latest session, so history exists only
if it is saved each day. `python fii_dii_log.py` appends (idempotent per date+category)
and prints the last 10 sessions. Figures are in Rs crore.
"""
import csv
import os
import sys
from datetime import datetime

import requests

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fii_dii_history.csv")
URL = "https://www.nseindia.com/api/fiidiiTradeReact"
FIELDS = ["date", "category", "buy", "sell", "net"]


def fetch():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0",
        "Accept": "application/json, text/plain, */*",
    })
    try:
        s.get("https://www.nseindia.com", timeout=15)
        r = s.get(URL, timeout=15)
        r.raise_for_status()
        return r.json()
    finally:
        s.close()


def load(path=PATH):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def append(rows, path=PATH):
    have = {(r["date"], r["category"]) for r in load(path)}
    new = []
    for r in rows:
        d = datetime.strptime(r["date"], "%d-%b-%Y").strftime("%Y-%m-%d")
        if (d, r["category"]) in have:
            continue
        new.append({"date": d, "category": r["category"], "buy": r["buyValue"],
                    "sell": r["sellValue"], "net": r["netValue"]})
    if new:
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if write_header:
                w.writeheader()
            w.writerows(new)
    return len(new)


def summary(path=PATH, n=10):
    by = {}
    for r in load(path):
        by.setdefault(r["date"], {})[r["category"]] = float(r["net"])
    for d in sorted(by)[-n:]:
        print(f"{d}  FII {by[d].get('FII/FPI', float('nan')):>9.0f}  DII {by[d].get('DII', float('nan')):>9.0f}")


if __name__ == "__main__":
    try:
        print(f"added {append(fetch())} row(s)")
    except Exception as e:
        print(f"fii_dii fetch failed: {e}")
        sys.exit(1)
    summary()

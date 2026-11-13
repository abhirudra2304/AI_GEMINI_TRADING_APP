"""NSE F&O participant-wise open interest (Client / DII / FII / Pro), appended to one history CSV.

Source (public, no auth, same family as nse_delivery_feed.py / fii_dii_log.py):
    https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv
Published after the close, one file per trading day, columns per participant:
    Future Index Long/Short, Future Stock Long/Short,
    Option Index Call/Put Long/Short, Option Stock Call/Put Long/Short, Total Long/Short.

    python fao_participant_oi.py              # fetch every missing trading day in the last 10 calendar days
    python fao_participant_oi.py --days 30    # wider backfill
    python fao_participant_oi.py --summary    # print the FII read without fetching

History file: fao_participant_oi_history.csv  (long format: date, client, <12 OI columns + totals>).
Weekends and holidays have no file and are skipped silently. Wired into scripts/run_nightly.ps1.
Positioning read (summary()) is derived only from this file; nothing is estimated.
"""
import argparse
import io
import os
import sys
from datetime import date, timedelta

import pandas as pd
import requests

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fao_participant_oi_history.csv")
URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
           "Accept": "text/csv, */*"}

COLS = ["fut_idx_long", "fut_idx_short", "fut_stk_long", "fut_stk_short",
        "opt_idx_call_long", "opt_idx_put_long", "opt_idx_call_short", "opt_idx_put_short",
        "opt_stk_call_long", "opt_stk_put_long", "opt_stk_call_short", "opt_stk_put_short",
        "total_long", "total_short"]


def _parse(text, day):
    """Parse one NSE file. Title line(s) precede the header row that contains 'Client Type'."""
    lines = text.splitlines()
    h = next((i for i, ln in enumerate(lines) if "client type" in ln.lower()), None)
    if h is None:
        raise ValueError("header row 'Client Type' not found - NSE layout changed")
    df = pd.read_csv(io.StringIO("\n".join(lines[h:])))
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={df.columns[0]: "client"})
    df["client"] = df["client"].astype(str).str.strip().str.upper()
    df = df[df["client"].isin(["CLIENT", "DII", "FII", "PRO", "TOTAL"])]
    if len(df.columns) != 1 + len(COLS):
        raise ValueError(f"expected {1 + len(COLS)} columns, got {len(df.columns)}: {list(df.columns)}")
    df.columns = ["client"] + COLS
    for c in COLS:
        df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce")
    if df[COLS].isna().any().any() or len(df) < 4:
        raise ValueError("unparseable values / missing participants")
    df.insert(0, "date", day.isoformat())
    return df


def fetch_day(day, session):
    """DataFrame for `day`, or None when NSE has no file (weekend/holiday/not yet published)."""
    r = session.get(URL.format(d=day.strftime("%d%m%Y")), timeout=20)
    if r.status_code in (403, 404) or not r.text.strip() or "<html" in r.text[:200].lower():
        return None
    r.raise_for_status()
    return _parse(r.text, day)


def load(path=PATH):
    return pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=["date", "client"] + COLS)


def update(days=10, path=PATH):
    have = set(load(path)["date"].astype(str))
    s = requests.Session()
    s.headers.update(HEADERS)
    new, missing = [], []
    for k in range(days, -1, -1):
        d = date.today() - timedelta(days=k)
        if d.weekday() >= 5 or d.isoformat() in have:
            continue
        try:
            df = fetch_day(d, s)
        except Exception as e:  # report, never guess
            print(f"{d} ERR {type(e).__name__}: {e}")
            missing.append(d)
            continue
        if df is None:
            missing.append(d)
        else:
            new.append(df)
    if new:
        out = pd.concat([load(path)] + new).drop_duplicates(["date", "client"], keep="last").sort_values(["date", "client"])
        tmp = path + ".tmp"
        out.to_csv(tmp, index=False)
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written history
    print(f"fao_participant_oi: added {len(new)} day(s); no file for {len(missing)} weekday(s) "
          f"({', '.join(str(m) for m in missing[-5:]) or '-'}) - holidays or not yet published")
    return len(new)


def summary(path=PATH, n=5):
    """Dict for the latest day: FII index-futures long/short, day-over-day OI change, index-option bias."""
    h = load(path)
    f = h[h["client"] == "FII"].sort_values("date").tail(n + 1)
    if len(f) == 0:
        return None
    last = f.iloc[-1]
    prev = f.iloc[-2] if len(f) > 1 else None

    def net(r):
        return r.fut_idx_long - r.fut_idx_short

    def opt_bias(r):  # net bullish index-option exposure in contracts: long calls + short puts - short calls - long puts
        return (r.opt_idx_call_long + r.opt_idx_put_short) - (r.opt_idx_call_short + r.opt_idx_put_long)

    out = {"date": last["date"],
           "idx_fut_long": int(last.fut_idx_long), "idx_fut_short": int(last.fut_idx_short),
           "idx_fut_ls_ratio": float(round(last.fut_idx_long / last.fut_idx_short, 3)),
           "idx_fut_long_pct": float(round(100 * last.fut_idx_long / (last.fut_idx_long + last.fut_idx_short), 1)),
           "idx_fut_net": int(net(last)),
           "idx_fut_net_chg": int(net(last) - net(prev)) if prev is not None else None,
           "stk_fut_net": int(last.fut_stk_long - last.fut_stk_short),
           "stk_fut_net_chg": int((last.fut_stk_long - last.fut_stk_short) - (prev.fut_stk_long - prev.fut_stk_short)) if prev is not None else None,
           "idx_opt_bias": int(opt_bias(last)),
           "idx_opt_bias_chg": int(opt_bias(last) - opt_bias(prev)) if prev is not None else None,
           "history_days": len(f)}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    if not a.summary:
        update(a.days)
    sm = summary()
    print(sm if sm else "no history yet")
    sys.exit(0)

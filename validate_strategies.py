"""Stage-4 validation: net-of-cost stats per strategy x market regime + bias audit.

Usage: python validate_strategies.py [--notional 100000] [--out reports/validation_<date>.md]

Read-only: reads signals.db (outcomes + signals), emfb_report_*.parquet, daily/15m parquet caches.
Does NOT modify backtest.py or the DB. Trade paths come from backtest.py's 15-minute replay
(same evaluators as production); this script only RE-COSTS them (itemised fees + slippage)
and tags regimes. EMFB is not in signals.db, so it is rebuilt from saved EMFB reports.
Cost rates are assumptions for a discount broker - verify against your contract note.
"""
import argparse
import glob
import math
import os
import sqlite3
import sys
from datetime import datetime

import numpy as np
import pandas as pd

# ---- cost assumptions (fractions of turnover unless noted) ----
GST = 0.18
EXCH_TXN = 0.0000297      # NSE equity
SEBI = 0.000001           # Rs10 / crore
DELIVERY = {"stt_buy": 0.001, "stt_sell": 0.001, "stamp_buy": 0.00015, "brokerage_flat": 0.0, "dp_sell_rs": 15.93}
INTRADAY = {"stt_buy": 0.0, "stt_sell": 0.00025, "stamp_buy": 0.00003, "brokerage_cap_rs": 20.0, "brokerage_pct": 0.0003}
DELIVERY_STRATS = {"BTST", "SWING", "EMFB"}   # BTST is held overnight -> delivery charges
SLIPPAGE_TIERS = [(100e7, 0.0005), (50e7, 0.0010), (10e7, 0.0020), (0, 0.0040)]  # per side, by 20d traded value
SLIPPAGE_UNKNOWN = 0.0020
MIN_N = 30


def itemised_cost_frac(strategy: str, entry: float, exit_: float, notional: float = 100_000.0) -> float:
    """Round-trip fees (excl. slippage) as a fraction of notional."""
    qty = max(1.0, math.floor(notional / entry))
    buy, sell = qty * entry, qty * exit_
    if strategy.upper() in DELIVERY_STRATS:
        p = DELIVERY
        brokerage = p["brokerage_flat"] * 2
        stt = buy * p["stt_buy"] + sell * p["stt_sell"]
        stamp = buy * p["stamp_buy"]
        dp = p["dp_sell_rs"]
    else:
        p = INTRADAY
        brokerage = min(p["brokerage_cap_rs"], buy * p["brokerage_pct"]) + min(p["brokerage_cap_rs"], sell * p["brokerage_pct"])
        stt = buy * p["stt_buy"] + sell * p["stt_sell"]
        stamp = buy * p["stamp_buy"]
        dp = 0.0
    exch = (buy + sell) * EXCH_TXN
    sebi = (buy + sell) * SEBI
    gst = GST * (brokerage + exch + sebi)
    return (brokerage + stt + stamp + dp + exch + sebi + gst) / buy


def slippage_per_side(avg_traded_value) -> float:
    if avg_traded_value is None or not np.isfinite(avg_traded_value):
        return SLIPPAGE_UNKNOWN
    for floor, s in SLIPPAGE_TIERS:
        if avg_traded_value >= floor:
            return s
    return SLIPPAGE_TIERS[-1][1]


def regime_label(nifty: pd.DataFrame, signal_date: pd.Timestamp) -> str:
    """Point-in-time Nifty trend regime using only bars strictly BEFORE signal_date."""
    d = nifty[nifty["_d"] < signal_date]
    # Nifty cache holds only ~300 daily bars (from 2025-09), so EMA200 is unavailable for
    # most of the sample; use EMA20/EMA50 (needs 60 bars) instead.
    if len(d) < 60:
        return "UNKNOWN"
    c = d["Close"]
    ema20, ema50 = c.ewm(span=20, adjust=False).mean().iloc[-1], c.ewm(span=50, adjust=False).mean().iloc[-1]
    last = c.iloc[-1]
    if last > ema50 and ema20 > ema50:
        return "BULL"
    if last < ema50 and ema20 < ema50:
        return "BEAR"
    return "MIXED"


def stats(r: pd.Series) -> dict:
    """r = net R-multiples in chronological order."""
    r = r.dropna()
    n = len(r)
    if n == 0:
        return {"n": 0}
    wins, losses = r[r > 0], r[r <= 0]
    eq = r.cumsum()
    eq0 = pd.concat([pd.Series([0.0]), eq.reset_index(drop=True)], ignore_index=True)  # start from 0
    dd = (eq0 - eq0.cummax()).min()
    sd = r.std(ddof=1) if n > 1 else float("nan")
    t = r.mean() / (sd / math.sqrt(n)) if n > 1 and sd and sd > 0 else float("nan")
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    return {"n": n, "win%": 100 * len(wins) / n, "avgR": r.mean(), "expectancy_R": r.mean(), "medianR": r.median(),
            "PF": pf, "maxDD_R": dd, "t": t, "reliable": n >= MIN_N}


_DAILY = {}


def _liquidity(symbol: str, before: pd.Timestamp):
    try:
        if symbol not in _DAILY:
            from grind_scanner import _daily_cache_path
            p = _daily_cache_path(symbol)
            d = pd.read_parquet(p) if p else None
            _DAILY[symbol] = None if d is None else d.assign(
                _d=pd.to_datetime(d["Timestamp"]).dt.tz_localize(None).dt.normalize())
        d = _DAILY[symbol]
        if d is None:
            return None
        d = d[d["_d"] < before].tail(20)
        return float((d["Close"] * d["Volume"]).mean()) if len(d) >= 10 else None
    except Exception:
        return None


def load_nifty() -> pd.DataFrame:
    from grind_scanner import _daily_cache_path
    d = pd.read_parquet(_daily_cache_path("Nifty 50"))
    d = d.assign(_d=pd.to_datetime(d["Timestamp"]).dt.tz_localize(None).dt.normalize()).sort_values("_d")
    return d


def load_db_trades(db: str = "signals.db") -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return pd.read_sql(
            "SELECT signal_id, strategy, symbol, signal_timestamp, entry, stop, target, exit_price, exit_time, entry_time, "
            "outcome, return_decimal, friction_decimal FROM outcomes WHERE outcome NOT LIKE 'SKIP%' "
            "AND entry > stop AND entry > 0", conn)
    finally:
        conn.close()


def build_emfb_trades() -> pd.DataFrame:
    """Replay EMFB report picks (deduped per symbol/day, best rank) with backtest.py's own evaluator."""
    import backtest
    frames = []
    for f in sorted(glob.glob("emfb_report_*.parquet")):
        try:
            frames.append(pd.read_parquet(f))
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.rename(columns={"Symbol": "symbol"})
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    df["_day"] = df["timestamp"].dt.normalize()
    df = df.sort_values(["_day", "Rank"]).drop_duplicates(["_day", "symbol"], keep="first").reset_index(drop=True)
    df["id"] = 900000 + df.index
    df["strategy"] = "EMFB"
    store = backtest.CandleStore()
    out = []
    for _, row in df.iterrows():
        try:
            r = backtest.evaluate_signal(row, store)
        except Exception:
            continue
        if str(r.outcome).startswith("SKIP") or r.entry <= r.stop:
            continue
        out.append({"signal_id": r.signal_id, "strategy": "EMFB", "symbol": r.symbol, "signal_timestamp": r.signal_timestamp,
                    "entry": r.entry, "stop": r.stop, "target": r.target, "exit_price": r.exit_price, "exit_time": r.exit_time,
                    "entry_time": r.entry_time, "outcome": r.outcome, "return_decimal": r.return_decimal,
                    "friction_decimal": r.friction_decimal})
    return pd.DataFrame(out)


def cost_trades(t: pd.DataFrame, nifty: pd.DataFrame, notional: float) -> pd.DataFrame:
    rows = []
    for r in t.itertuples(index=False):
        sig_ts = pd.Timestamp(r.signal_timestamp).tz_localize(None) if pd.Timestamp(r.signal_timestamp).tzinfo else pd.Timestamp(r.signal_timestamp)
        day = sig_ts.normalize()
        risk_frac = (r.entry - r.stop) / r.entry
        if pd.isna(r.exit_price):
            continue
        gross = r.exit_price / r.entry - 1.0
        fees = itemised_cost_frac(r.strategy, r.entry, r.exit_price, notional)
        slip = 2 * slippage_per_side(_liquidity(r.symbol, day))
        net = gross - fees - slip
        et = pd.Timestamp(r.entry_time).tz_localize(None) if pd.Timestamp(r.entry_time).tzinfo else pd.Timestamp(r.entry_time)
        rows.append({"strategy": r.strategy, "symbol": r.symbol, "day": day, "entry_time": et, "signal_ts": sig_ts,
                     "outcome": r.outcome, "gross": gross, "fees": fees, "slip": slip, "net": net,
                     "flat_net": gross - r.friction_decimal, "grossR": gross / risk_frac, "netR": net / risk_frac,
                     "regime": regime_label(nifty, day), "lookahead_ok": et > sig_ts})
    return pd.DataFrame(rows).sort_values("entry_time").reset_index(drop=True)


def table(t: pd.DataFrame) -> list:
    hdr = ["| Strategy / regime | n | Win% | Avg net R | Expectancy | PF | MaxDD (R) | t-stat |",
           "|---|---|---|---|---|---|---|---|"]
    lines = hdr[:]
    for strat in ["BTST", "INTRADAY", "GAP", "SWING", "EMFB"]:
        g = t[t.strategy == strat]
        for reg in ["ALL", "BULL", "MIXED", "BEAR", "UNKNOWN"]:
            sub = g if reg == "ALL" else g[g.regime == reg]
            s = stats(sub["netR"])
            label = f"{strat} / {reg}"
            if s.get("n", 0) == 0:
                lines.append(f"| {label} | 0 | - | - | - | - | - | - |")
                continue
            flag = "" if s["reliable"] else " (n<30 UNRELIABLE)"
            lines.append(f"| {label}{flag} | {s['n']} | {s['win%']:.1f} | {s['avgR']:+.3f} | {s['avgR'] * 100:+.1f}% of risk | "
                         f"{s['PF']:.2f} | {s['maxDD_R']:.1f} | {s['t']:+.2f} |")
    return lines


def cost_impact(t: pd.DataFrame) -> list:
    lines = ["| Strategy | n | Gross avg % | Flat-cost net % (backtest.py) | Itemised+slippage net % | Avg fees % | Avg slippage % |",
             "|---|---|---|---|---|---|---|"]
    for strat, g in t.groupby("strategy"):
        lines.append(f"| {strat} | {len(g)} | {100 * g.gross.mean():+.3f} | {100 * g.flat_net.mean():+.3f} | {100 * g.net.mean():+.3f} | "
                     f"{100 * g.fees.mean():.3f} | {100 * g.slip.mean():.3f} |")
    return lines


def audit(t: pd.DataFrame) -> list:
    span = f"{t.day.min().date()} to {t.day.max().date()}"
    ndays = t.day.nunique()
    reg_share = t.regime.value_counts(normalize=True).mul(100).round(0).to_dict()
    per_day = t.groupby(["strategy", "day"]).size().groupby("strategy").mean().round(1).to_dict()
    la = int((~t.lookahead_ok).sum())
    return [
        f"- **Sample window**: {span}, {ndays} distinct signal days (~{ndays / 21:.0f} months). One market cycle at most; regime split is thin. Regime mix of trades: {reg_share}.",
        f"- **Clustering / effective sample size**: avg signals per day per strategy {per_day}. Trades on the same day are highly correlated (same market move), so effective n is closer to the number of days than to the trade count; t-stats here are OVERSTATED.",
        f"- **Look-ahead (entry vs signal time)**: {la} trades have entry_time <= signal time (expect 0). Entries are the NEXT candle open after the signal; stops/targets are re-anchored to the actual fill; same-candle stop/target ties resolve to STOP. Regime tag uses Nifty bars strictly before the signal day. Signal-generation look-ahead (indicators computed on a bar that includes data after the signal timestamp, e.g. mid-session daily bars) is NOT excluded by this script: the provisional-daily-bar bug (fixed 2026-09-10) affected cached daily closes before that date.",
        "- **Survivorship / universe**: signals come from today's TARGET_UNIVERSE (222 names, hand-curated and swapped over time, 3-for-3 swap 2026-08-28; +6 recent IPOs 2026-09-25, +ADANIPORTS/MINDACORP/RELIANCE etc. in November). The universe was chosen with hindsight (memory: config-215 hindsight bias), and delisted/collapsed names cannot appear. Results are biased UPWARD.",
        "- **Corporate actions**: backtest.py states it does not adjust for splits/bonuses (known critical flaw). Any symbol with an action inside the replay window can show a phantom gap loss or gain.",
        "- **Overfitting / parameter search**: stops/targets (ATR 1.5-2.0x, R:R 1.5-2.0), thresholds and universe changes were tuned over the same months now used to test them. No holdout, no walk-forward in this script. Treat any positive number as in-sample.",
        "- **Costs**: itemised fees use assumed discount-broker rates (verify). Slippage is a tier assumption by 20d traded value, not measured fills. Entry at next 15-min candle OPEN is optimistic for fast movers; BTST exit is a close-of-next-session time stop, so exit slippage on illiquid names is likely understated. Position impact beyond Rs 1 lakh is ignored.",
        "- **Strategy coverage**: GAP and INTRADAY have a handful of days of signals in the DB; EMFB is rebuilt from saved reports (post-close 15:45+ timestamps, enters next session) and is not what the live system persisted. Small n marks them UNRELIABLE.",
        "- **Regime definition**: Nifty close vs EMA50 and EMA20 vs EMA50 (point-in-time; the Nifty cache has only ~300 bars so EMA200 was unusable; UNKNOWN = under 60 prior bars). `MarketContextEngine` (breadth/VIX/liquidity) cannot be reproduced historically because breadth/AD snapshots are not stored per day, so it is NOT used here.",
        "- **Prior evidence**: memory records no bias-free edge in walk-forward (324 sessions), SWING score inversely related to outcomes (r=-0.052), BTST negative in every month. This report should be read against that, not as new evidence of an edge.",
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notional", type=float, default=100_000.0)
    ap.add_argument("--out", default=os.path.join("reports", f"validation_{datetime.now():%Y%m%d}.md"))
    ap.add_argument("--skip-emfb", action="store_true")
    a = ap.parse_args(argv)
    nifty = load_nifty()
    trades = load_db_trades()
    if not a.skip_emfb:
        trades = pd.concat([trades, build_emfb_trades()], ignore_index=True)
    t = cost_trades(trades, nifty, a.notional)
    md = [f"# Strategy validation - {datetime.now():%Y-%m-%d %H:%M}", "",
          f"Net of itemised fees + slippage, notional Rs {a.notional:,.0f}/trade. R = net return / planned risk (entry-stop). "
          "Informational only; not advice. See audit before trusting any number.", "",
          "## Results by strategy and regime", ""] + table(t) + ["", "## Cost impact vs backtest.py's flat cost", ""] + cost_impact(t) + \
         ["", "## Bias and reliability audit", ""] + audit(t) + ["", "## Reliability verdict", "",
          "- Not reliable for deployment: single short window, hindsight universe, unadjusted corporate actions, correlated same-day trades, no holdout.",
          "- Rows marked n<30 carry no inferential weight."]
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\nwritten: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

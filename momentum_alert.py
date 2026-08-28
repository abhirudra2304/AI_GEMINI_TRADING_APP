"""
Momentum Drop Alert (IAS Plug-in Module)

Watches a small, user-maintained watchlist of held positions (see
watchlist_positions.yaml) for the "distribution" signature - price breaking
below its EMA20 while volume expands above its recent average - as opposed
to ordinary single-day price noise.

Built 2026-08-19 after the user asked to be warned automatically if a held
position's momentum starts dropping, following a session where HAL and
ZENTEC both showed a fading EMFB/momentum score while price stayed
elevated. Deliberately conservative: requires BOTH a volume spike AND an
EMA20 break together, not either alone - verified against 2026-08-19's
market-wide red day (Nifty -0.41%), where every held position dipped on
normal-to-light volume while still holding EMA20. A single-condition
trigger would have false-alarmed that day; this design would have
correctly stayed silent.

Design notes
------------
- Completely independent: no imports from scanner_engine.py, emfb.py,
  momentum_scanner.py, or orchestrator.py, and nothing in the existing
  scan pipeline imports this one except main.py's run_eod(), which only
  reads its output (a DataFrame) - adding it carries no regression risk
  to the existing BTST/SWING/EMFB scan logic.
- Informational/alert-only: this module NEVER places, sizes, or suggests
  an order. It answers exactly one question per watchlist symbol - "is
  today's price action the distribution signature, or not" - and leaves
  every decision to the user.
- Config-driven: the watchlist, volume threshold, and lookback window all
  live in watchlist_positions.yaml, not hardcoded here. Same
  never-crash-on-bad-config fallback pattern as
  momentum_scanner._load_momentum_config.
- Failure policy: any problem for an individual symbol (fetch failure,
  insufficient history, rate limit) is logged and that symbol is simply
  omitted from the result - never raises, never blocks the other symbols
  or the caller (run_eod()).
"""

import logging
import os

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

WATCHLIST_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist_positions.yaml")

_DEFAULT_CONFIG = {
    "watchlist": [],
    "volume_ratio_threshold": 1.3,
    "lookback_days": 40,
}


def _load_watchlist_config() -> dict:
    """Loads watchlist_positions.yaml, falling back to an empty watchlist on
    any missing/malformed file - mirrors momentum_scanner._load_momentum_config's
    fallback behavior so this plugin never crashes on a bad/missing config."""
    if not os.path.exists(WATCHLIST_CONFIG_PATH):
        logger.warning(f"{WATCHLIST_CONFIG_PATH} not found; momentum alert has nothing to watch.")
        return _DEFAULT_CONFIG
    try:
        with open(WATCHLIST_CONFIG_PATH, "r") as f:
            loaded = yaml.safe_load(f)
        if not isinstance(loaded, dict):
            raise ValueError("top-level YAML content is not a mapping")
        merged = {**_DEFAULT_CONFIG, **loaded}
        return merged
    except Exception as e:
        logger.warning(f"Failed to parse {WATCHLIST_CONFIG_PATH} ({e}); momentum alert has nothing to watch.")
        return _DEFAULT_CONFIG


def check_momentum_drops(broker) -> pd.DataFrame:
    """Checks every symbol in watchlist_positions.yaml for the distribution
    signature (EMA20 break + volume expansion together) using the most
    recent daily candle.

    Args:
        broker: a DataBroker instance (reused from the caller, e.g.
            run_eod(), rather than opening a new session here).

    Returns:
        DataFrame with one row per FLAGGED symbol only - columns Symbol,
        Close, EMA20, Pct_Below_EMA20, Volume, Avg_Volume_20d, Volume_Ratio.
        Empty DataFrame (never raises) if nothing is flagged, the watchlist
        is empty, or every fetch fails.
    """
    cfg = _load_watchlist_config()
    watchlist = cfg.get("watchlist") or []
    if not watchlist:
        return pd.DataFrame()

    vol_threshold = cfg.get("volume_ratio_threshold", _DEFAULT_CONFIG["volume_ratio_threshold"])
    lookback_days = cfg.get("lookback_days", _DEFAULT_CONFIG["lookback_days"])

    flagged = []
    for symbol in watchlist:
        try:
            df = broker.fetch_ohlcv(symbol, "ONE_DAY", lookback_days, caller="MomentumAlert")
            if df is None or df.empty or len(df) < 21:
                logger.warning(f"Momentum alert skipped for {symbol}: insufficient history.")
                continue

            df = df.sort_values("Timestamp").reset_index(drop=True)
            df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
            last = df.iloc[-1]
            avg_volume_20d = df["Volume"].tail(21).iloc[:-1].mean()
            if avg_volume_20d <= 0:
                continue

            volume_ratio = last["Volume"] / avg_volume_20d
            below_ema20 = last["Close"] < last["EMA20"]

            if below_ema20 and volume_ratio >= vol_threshold:
                pct_below = (last["Close"] - last["EMA20"]) / last["EMA20"] * 100
                flagged.append({
                    "Symbol": symbol,
                    "Close": round(float(last["Close"]), 2),
                    "EMA20": round(float(last["EMA20"]), 2),
                    "Pct_Below_EMA20": round(float(pct_below), 2),
                    "Volume": int(last["Volume"]),
                    "Avg_Volume_20d": int(avg_volume_20d),
                    "Volume_Ratio": round(float(volume_ratio), 2),
                })
        except Exception as e:
            logger.warning(f"Momentum alert check failed for {symbol}: {e}")
            continue

    return pd.DataFrame(flagged)

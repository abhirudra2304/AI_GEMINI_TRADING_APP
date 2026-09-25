"""Tradability flag: NSE segment (EQ vs BE trade-for-trade) + surveillance (ASM/GSM).

Why this exists (found 2026-09-26): 10 of the 215 universe names were under NSE
surveillance, and E2E/HFCL/MTARTECH/STLTECH were trading in the BE (trade-for-trade)
series - no intraday, compulsory delivery, so a BTST sale before the shares reach
demat risks short delivery. Nothing in the pipeline checked this, and E2E sat in
that week's BTST/SWING lists. Angel's scrip master is NOT a usable source for the
series - it disagreed with NSE on MTARTECH and IDEAFORGE the same day - so the
series comes from NSE's own daily sec_bhavdata_full file.

Flags (one per symbol):
    OK       EQ series, no surveillance
    CAUTION  ASM Stage I-II (higher margin; can escalate)
    NO_BTST  BE series, ASM Stage III+, or any GSM stage
    UNKNOWN  status could not be determined - never silently treated as OK

Informational in stockscan; the decision brief drops NO_BTST from its BTST section
only (visibly, with a count), keeping them flagged in SWING/GRIND.
"""
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests

import config
from nse_announcements import NSE_HEADERS

logger = logging.getLogger(__name__)

CACHE_PATH = "tradability_cache.json"
DELIVERY_CACHE = "delivery_cache"
MAX_SERIES_AGE_DAYS = 4
MAX_FALLBACK_CACHE_AGE_DAYS = 3
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}


def _stage_number(text: str) -> Optional[int]:
    m = re.search(r"Stage\s+([IVX]+)\b", text or "")
    return _ROMAN.get(m.group(1)) if m else None


def build_status(series: pd.DataFrame, asm_rows: list, gsm_rows: list) -> pd.DataFrame:
    """Pure function: series = DataFrame[SYMBOL, SERIES] (EQ/BE rows of one NSE day);
    asm_rows / gsm_rows = NSE API row dicts (need 'symbol' plus asmSurvIndicator/survDesc)."""
    ser = (series.assign(SYMBOL=series["SYMBOL"].str.strip(), SERIES=series["SERIES"].str.strip())
           .drop_duplicates("SYMBOL").set_index("SYMBOL")["SERIES"])
    surv: dict = {}
    for r in asm_rows:
        stage = _stage_number(r.get("asmSurvIndicator") or r.get("survDesc", ""))
        kind = "LTASM" if "Long Term" in (r.get("survDesc") or "") else "STASM"
        surv[r["symbol"].strip()] = (kind, stage)
    for r in gsm_rows:
        surv[r["symbol"].strip()] = ("GSM", None)

    rows = []
    for sym in sorted(set(ser.index) | set(surv)):
        s = ser.get(sym)
        kind, stage = surv.get(sym, (None, None))
        label = "" if kind is None else (f"{kind} Stage {stage}" if stage else kind)
        if s == "BE" or kind == "GSM" or (stage is not None and stage >= 3):
            flag = "NO_BTST"
        elif kind is not None:
            flag = "CAUTION"
        elif s == "EQ":
            flag = "OK"
        else:
            flag = "UNKNOWN"
        note = "; ".join(x for x in [
            "trade-for-trade (BE): no intraday, delivery compulsory" if s == "BE" else "",
            f"surveillance {label}" if label else "",
        ] if x)
        rows.append({"Symbol": sym, "Series": s or "", "Surveillance": label,
                     "Tradability_Flag": flag, "Tradability_Note": note})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- fetch
def _latest_series_from_cache() -> Optional[pd.DataFrame]:
    if not os.path.isdir(DELIVERY_CACHE):
        return None
    files = sorted(f for f in os.listdir(DELIVERY_CACHE) if f.endswith(".parquet"))
    if not files:
        return None
    day = datetime.strptime(files[-1][:8], "%Y%m%d")
    if (datetime.now() - day).days > MAX_SERIES_AGE_DAYS:
        return None
    df = pd.read_parquet(os.path.join(DELIVERY_CACHE, files[-1]), columns=["SYMBOL", "SERIES"])
    df.attrs["as_of"] = day.strftime("%Y-%m-%d")
    return df


def _latest_series_from_nse(session: requests.Session) -> Optional[pd.DataFrame]:
    d = datetime.now(config.MARKET_TZ).date()
    for _ in range(MAX_SERIES_AGE_DAYS + 3):
        if d.weekday() < 5:
            r = session.get(f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv",
                            headers=NSE_HEADERS, timeout=30)
            if r.status_code == 200:
                try:
                    df = pd.read_csv(io.StringIO(r.text)); df.columns = df.columns.str.strip()
                    df = df[df["SERIES"].str.strip().isin(["EQ", "BE"])][["SYMBOL", "SERIES"]]
                    df.attrs["as_of"] = d.isoformat()
                    return df
                except Exception:
                    pass
        d -= timedelta(days=1)
    return None


def _fetch_surveillance(session: requests.Session) -> tuple:
    session.get("https://www.nseindia.com/reports/asm", headers=NSE_HEADERS, timeout=20)
    asm = session.get("https://www.nseindia.com/api/reportASM", headers=NSE_HEADERS, timeout=20).json()
    gsm = session.get("https://www.nseindia.com/api/reportGSM", headers=NSE_HEADERS, timeout=20).json()
    asm_rows = [r for k in ("longterm", "shortterm") for r in (asm.get(k) or {}).get("data", [])]
    return asm_rows, (gsm if isinstance(gsm, list) else [])


def load_status(force: bool = False) -> tuple:
    """Returns (status DataFrame or None, human-readable source note). Fetches at most
    once per calendar day; falls back to a cache up to 3 days old, else None."""
    today = datetime.now(config.MARKET_TZ).strftime("%Y-%m-%d")
    cached = None
    if os.path.exists(CACHE_PATH):
        try:
            cached = json.load(open(CACHE_PATH))
        except Exception:
            cached = None
    if cached and cached.get("date") == today and not force:
        return pd.DataFrame(cached["rows"]), cached["note"]
    try:
        s = requests.Session()
        series = _latest_series_from_cache()
        if series is None:
            series = _latest_series_from_nse(s)
        if series is None:
            raise RuntimeError("no NSE series file within the last few sessions")
        asm_rows, gsm_rows = _fetch_surveillance(s)
        status = build_status(series, asm_rows, gsm_rows)
        note = f"NSE series {series.attrs.get('as_of')} + ASM/GSM lists fetched {today}"
        json.dump({"date": today, "note": note, "rows": status.to_dict("records")}, open(CACHE_PATH, "w"))
        return status, note
    except Exception as e:
        logger.warning(f"Tradability fetch failed ({e}).")
        if cached:
            age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(cached["date"], "%Y-%m-%d")).days
            if age <= MAX_FALLBACK_CACHE_AGE_DAYS:
                return pd.DataFrame(cached["rows"]), f"{cached['note']} (CACHED {age}d old - live fetch failed)"
        return None, f"UNAVAILABLE ({e}) - all names marked UNKNOWN"


def annotate_tradability(df: pd.DataFrame, status: Optional[pd.DataFrame], symbol_col: str = "Symbol") -> pd.DataFrame:
    """Left-joins Tradability_Flag / Tradability_Note. A name absent from the status (or no
    status at all) is UNKNOWN - never OK."""
    out = df.drop(columns=[c for c in ("Tradability_Flag", "Tradability_Note") if c in df.columns])
    if status is None or status.empty or out.empty:
        out["Tradability_Flag"] = "UNKNOWN"
        out["Tradability_Note"] = ""
        return out
    m = status[["Symbol", "Tradability_Flag", "Tradability_Note"]].rename(columns={"Symbol": symbol_col})
    out = out.merge(m, on=symbol_col, how="left")
    out["Tradability_Flag"] = out["Tradability_Flag"].fillna("UNKNOWN")
    out["Tradability_Note"] = out["Tradability_Note"].fillna("not in today's NSE EQ/BE list (suspended/renamed?)")
    return out


if __name__ == "__main__":
    st, note = load_status(force=True)
    print(note)
    if st is not None:
        uni = st[st.Symbol.isin(config.Universe.TARGET_UNIVERSE)]
        print(uni[uni.Tradability_Flag != "OK"].to_string(index=False))
        missing = sorted(set(config.Universe.TARGET_UNIVERSE) - set(st.Symbol))
        print("universe names not found (UNKNOWN):", missing)

"""Fundamentals and institutional-holding table from NSE's own disclosures.

Standalone and read-only like strategy_paper.py / medium_rank.py: no broker API, no scanner
changes. Writes fundamentals/fundamentals_<date>.csv and caches raw responses per symbol per
day in fundamentals_cache/ (git-ignored).

    python fundamentals.py                    # tier A+B of the latest medium_rank CSV + holdings
    python fundamentals.py SOLARINDS HAL      # specific symbols

Sources (NSE public endpoints):
  - integrated-filing-results -> each quarter's results filing (iXBRL): revenue, total income,
    profit, profit attributable to owners, debt/equity. CONSOLIDATED is used where the company
    files it, else standalone (Basis column); the latest revision of a quarter wins.
  - corporate-share-holdings-master -> quarterly shareholding XBRL: promoter, mutual funds,
    domestic institutions (DII), foreign portfolio investors (FPI), promoter pledge.
Columns: Inc_YoY% / PAT_YoY% = latest quarter vs the same quarter a year earlier;
MF_chg / FPI_chg / DII_chg = percentage-point change vs the previous quarter's filing.
Flags: GROWTH = revenue and PAT both up >= 15% YoY; INST_ADDING = MF + DII + FPI combined up
>= 0.5 pt QoQ; PLEDGE = promoter pledge > 5% of shares; LEVERED = debt/equity > 1 (only where the filing
reports the ratio). ROCE is not in these feeds - check it by hand.
"""
import glob
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime

import pandas as pd
import requests

from nse_announcements import NSE_HEADERS

CACHE_DIR = "fundamentals_cache"
OUT_DIR = "fundamentals"
HOLDINGS = ["DATAPATTNS", "DIXON", "SOLARINDS", "TCS", "BEL", "BDL", "HAL", "KAYNES", "CONCOR", "GODFRYPHLP"]
SHP_KEYS = {
    "Promoter%": "ShareholdingOfPromoterAndPromoterGroup_ContextI",
    "MF%": "MutualFundsOrUTI_ContextI",
    "DII%": "InstitutionsDomestic_ContextI",
    "FPI%": "InstitutionsForeign_ContextI",
}
_session = None


def _get(url, as_json=True):
    global _session
    if _session is None:
        _session = requests.Session(); _session.headers.update(NSE_HEADERS)
        _session.get("https://www.nseindia.com", timeout=20)
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=60)
            r.raise_for_status()
            time.sleep(0.8)
            return r.json() if as_json else r.text
        except Exception:
            time.sleep(3 * (attempt + 1))
            _session = None
            _session = requests.Session(); _session.headers.update(NSE_HEADERS)
            _session.get("https://www.nseindia.com", timeout=20)
    return None


_UNITS = {"lakhs": 1e-2, "lakh": 1e-2, "crores": 1.0, "crore": 1.0, "thousands": 1e-4, "millions": 0.1,
          "rupees": 1e-7, "rupee": 1e-7}


def _parse_ixbrl(text):
    """Values in Rs crore from an integrated-filing iXBRL page (first numeric column = the quarter)."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)   # 2025-format pages keep a stale "Lakhs" row in a comment
    rows = {}
    for tr in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        cells = [html.unescape(re.sub("<[^>]+>", "", c)).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
        cells = [re.sub(r"\s+", " ", c) for c in cells if c]
        for i, c in enumerate(cells[:-1]):
            if re.search(r"[A-Za-z]", c) and c not in rows:
                rows[c.lower()] = cells[i + 1]
                break
    unit = _UNITS.get((rows.get("level of rounding used in financial results") or "").lower(), None)
    if unit is None and "in actuals" in (rows.get("description of presentation currency") or "").lower():
        unit = 1e-7

    def val(*labels):
        for lab in labels:
            v = rows.get(lab)
            if v is None:
                continue
            v = v.replace(",", "")
            neg = v.startswith("(") and v.endswith(")")
            try:
                x = float(v.strip("()"))
            except ValueError:
                continue
            if x == 0 and lab != labels[-1]:
                continue   # some filers leave a line at 0 (e.g. owners-of-parent split) - try the next label
            return -x if neg else x
        return None

    out = {"end": rows.get("date of end of reporting period"), "unit_ok": unit is not None}
    u = unit or 1e-2
    for k, labs in {"revenue": ("revenue from operations", "interest earned", "total income"),
                    "income": ("total income",),
                    "pat": ("profit or loss, attributable to owners of parent", "total profit (loss) for period",
                            "net profit (loss) for the period", "net profit loss for the period")}.items():
        x = val(*labs)
        out[k] = round(x * u, 1) if x is not None else None
    out["de"] = val("debt equity ratio")
    return out


def _pick_results(filings):
    """Latest quarter and the same quarter a year earlier, consolidated where available."""
    by_q = {}
    for f in filings:
        if not f.get("qe_Date") or not f.get("ixbrl") or f.get("consolidated") not in ("Consolidated", "Standalone"):
            continue
        qd = datetime.strptime(f["qe_Date"], "%d-%b-%Y").date()
        key = (qd, f["consolidated"])
        cur = by_q.get(key)
        ts = datetime.strptime(f["creation_Date"], "%d-%b-%Y %H:%M:%S")
        if cur is None or ts > cur[0]:
            by_q[key] = (ts, f)
    quarters = sorted({q for q, _ in by_q}, reverse=True)
    if not quarters:
        return []
    latest = quarters[0]
    basis = "Consolidated" if (latest, "Consolidated") in by_q else "Standalone"
    out = []
    for q in [latest, latest.replace(year=latest.year - 1)] + quarters[1:2]:
        hit = by_q.get((q, basis))
        if hit and not any(r["quarter"] == q.isoformat() for r in out):
            vals = _parse_ixbrl(_get(hit[1]["ixbrl"], as_json=False) or "")
            out.append({"quarter": q.isoformat(), "basis": basis, "filed": hit[1].get("broadcast_Date") or hit[1]["creation_Date"], **vals})
    return out


def _cached(sym):
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, f"{sym}_{date.today():%Y%m%d}.json")
    if os.path.exists(p):
        return json.load(open(p))
    filings = (_get("https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol="
                    f"{sym}&type=Integrated%20Filing-%20Financials") or {}).get("data", [])
    results = _pick_results(filings)
    shp_list = _get(f"https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol={sym}") or []
    shp = []
    for row in sorted(shp_list, key=lambda r: datetime.strptime(r["date"], "%d-%b-%Y"), reverse=True)[:2]:
        xml = _get(row["xbrl"], as_json=False) if row.get("xbrl") else None
        vals = {}
        if xml:
            for k, ctx in SHP_KEYS.items():
                m = re.search(r'<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="%s"[^>]*>([^<]+)<' % ctx, xml)
                vals[k] = round(float(m.group(1)) * 100, 2) if m else None
            m = re.search(r'<in-bse-shp:EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI"[^>]*>([^<]+)<', xml)
            vals["Pledge%"] = round(float(m.group(1)) * 100, 2) if m else 0.0
        shp.append({"date": row["date"], **vals})
    out = {"results": results, "shp": shp}
    json.dump(out, open(p, "w"))
    return out


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _pct(a, b):
    return round((a / b - 1) * 100, 1) if a is not None and b not in (None, 0) and b > 0 else None


def row_for(sym):
    raw = _cached(sym)
    r = {"Symbol": sym}
    q = {x["quarter"]: x for x in raw["results"]}
    if q:
        lq = max(q)
        last = q[lq]
        ld = datetime.strptime(lq, "%Y-%m-%d").date()
        r["Quarter"] = ld.strftime("%b-%y")
        r["Basis"] = "Consol" if last["basis"] == "Consolidated" else "Standalone"
        r["Revenue_Cr"], r["PAT_Cr"] = last.get("revenue"), last.get("pat")
        if last.get("revenue") and last.get("pat") is not None:
            r["PAT_Margin%"] = round(last["pat"] / last["revenue"] * 100, 1)
        yago = q.get(ld.replace(year=ld.year - 1).isoformat())
        if yago:
            r["Rev_YoY%"] = _pct(last.get("revenue"), yago.get("revenue"))
            r["PAT_YoY%"] = _pct(last.get("pat"), yago.get("pat"))
        r["D/E"] = last.get("de")
        r["Results_Filed"] = (last.get("filed") or "")[:11]
    s = raw["shp"]
    if s:
        r["SHP_Qtr"] = datetime.strptime(s[0]["date"], "%d-%b-%Y").strftime("%b-%y")
        for k in ("Promoter%", "MF%", "DII%", "FPI%", "Pledge%"):
            r[k] = s[0].get(k)
        if len(s) > 1:
            for k in ("MF%", "DII%", "FPI%", "Promoter%"):
                a, b = s[0].get(k), s[1].get(k)
                r[k.replace("%", "_chg")] = round(a - b, 2) if a is not None and b is not None else None
    g = r.get("Rev_YoY%"), r.get("PAT_YoY%")
    flags = []
    if all(x is not None and x >= 15 for x in g):
        flags.append("GROWTH")
    elif any(x is not None and x < 0 for x in g):
        flags.append("SHRINKING")
    inst = [r.get("DII_chg"), r.get("FPI_chg")]   # DII already includes MF
    if all(x is not None for x in inst) and sum(inst) >= 0.5:
        flags.append("INST_ADDING")
    elif all(x is not None for x in inst) and sum(inst) <= -0.5:
        flags.append("INST_SELLING")
    if (r.get("Pledge%") or 0) > 5:
        flags.append("PLEDGE")
    if (r.get("D/E") or 0) > 1:
        flags.append("LEVERED")
    r["Flags"] = " ".join(flags)
    return r


def default_symbols():
    files = sorted(glob.glob(os.path.join("medium_rank", "medium_rank_*.csv")))
    syms = []
    if files:
        df = pd.read_csv(files[-1])
        syms = df[df["Tier"].isin(["A", "B"])]["Symbol"].tolist()
    return list(dict.fromkeys(syms + HOLDINGS))


def main(symbols):
    rows = []
    for i, s in enumerate(symbols, 1):
        try:
            rows.append(row_for(s))
        except Exception as e:
            rows.append({"Symbol": s, "Flags": f"ERROR {e}"})
        print(f"\r  {i}/{len(symbols)} {s:<12}", end="", flush=True)
    print()
    df = pd.DataFrame(rows)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"fundamentals_{date.today():%Y%m%d}.csv")
    df.to_csv(path, index=False)
    cols = ["Symbol", "Quarter", "Basis", "Revenue_Cr", "PAT_Cr", "PAT_Margin%", "Rev_YoY%", "PAT_YoY%", "D/E",
            "SHP_Qtr", "Promoter%", "MF%", "MF_chg", "DII_chg", "FPI%", "FPI_chg", "Pledge%", "Flags"]
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
    print(df[[c for c in cols if c in df.columns]].to_string(index=False))
    print(f"\nSaved {path}. Revenue/PAT in Rs crore from NSE integrated filings "
          "(consolidated where filed; PAT = attributable to owners); ROCE is not in these feeds.")


if __name__ == "__main__":
    main([s.upper() for s in sys.argv[1:]] or default_symbols())

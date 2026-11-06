"""Download quarterly shareholding history (MF / FPI / DII %) for a symbol list. Resumable:
one JSON per symbol in shp_cache/. Read-only against NSE; no broker calls.

    python research/inst_shp_download.py symbols.csv
"""
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fundamentals as f  # noqa: E402  (reuses its NSE session + polite pacing)

OUT = "shp_cache"
KEYS = {
    "MF": [("MutualFundsOrUTI_ContextI", 100), ("MutualFundsOrUtiI", 1)],
    "FPI": [("InstitutionsForeign_ContextI", 100), ("InstitutionsForeignI", 1),
            ("InstitutionsForeignPortfolioInvestorI", 1)],
    "DII": [("InstitutionsDomestic_ContextI", 100), ("InstitutionsDomesticI", 1)],
}


def parse(xml):
    out = {}
    for k, opts in KEYS.items():
        for ctx, mult in opts:
            m = re.search(r'ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="%s"[^>]*>([^<]+)<' % ctx, xml)
            if m:
                try:
                    out[k] = round(float(m.group(1)) * mult, 3)
                    out[k + "_key"] = ctx
                except ValueError:
                    pass
                break
    return out


def one(sym):
    p = os.path.join(OUT, f"{sym}.json")
    if os.path.exists(p):
        return "cached"
    rows = f._get("https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol="
                  + urllib.parse.quote(sym)) or []
    out = []
    for r in rows:
        if not r.get("xbrl"):
            continue
        xml = f._get(r["xbrl"], as_json=False)
        rec = {"date": r.get("date"), "broadcast": r.get("broadcastDate") or r.get("submissionDate")}
        if xml:
            rec.update(parse(xml))
        out.append(rec)
    json.dump(out, open(p, "w"))
    return f"{len(out)} qtrs, MF parsed {sum('MF' in r for r in out)}"


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    syms = [l.strip() for l in open(sys.argv[1]) if l.strip() and l.strip() != "0"]
    t0 = datetime.now()
    for i, s in enumerate(syms, 1):
        try:
            msg = one(s)
        except Exception as e:  # keep going; a rerun retries anything without a cache file
            msg = f"ERR {type(e).__name__}"
        print(f"{i}/{len(syms)} {s}: {msg}  [{(datetime.now() - t0).seconds // 60} min]", flush=True)

"""
Standalone utility to refresh the local Angel One symbol -> token map used by
DataBroker's rolling-beta pipeline (see fetch_daily_candles / calculate_rolling_beta
in data_broker.py). Run this periodically to keep angel_tokens.json current with
Angel One's live scrip master (new listings, delistings, token changes).

Usage: python update_tokens.py
"""
import json
import logging
import sys
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
OUTPUT_PATH = "angel_tokens.json"
# '26000' is a commonly-cited but incorrect NIFTY 50 token for Angel One's
# SmartAPI; the real index token (verified against their own scrip master) is
# '99926000' - getCandleData silently returns an empty (not erroring) response
# for the wrong one, so this is easy to get wrong without noticing.
NIFTY_50_TOKEN = "99926000"


def download_scrip_master(url: str = SCRIP_MASTER_URL, timeout: int = 30) -> List[Dict[str, Any]]:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def build_token_map(scrip_master: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Maps NSE equity tickers to their numeric token, plus the NIFTY_50 benchmark
    token. Includes both the '-EQ' (normal) and '-BE' (trade-to-trade /
    surveillance) settlement series - a stock under a T2T restriction is still
    real and tradeable, so excluding '-BE' entirely would silently starve
    calculate_rolling_beta for any ticker currently under that status, which is
    unrelated to whether its beta is computable. When a ticker briefly appears
    in both series, '-EQ' wins since it's the primary one.
    """
    token_map: Dict[str, str] = {}
    be_only: Dict[str, str] = {}
    for entry in scrip_master:
        symbol = entry.get('symbol', '')
        token = entry.get('token')
        if entry.get('exch_seg') != 'NSE' or not isinstance(symbol, str) or not token:
            continue
        if symbol.endswith('-EQ'):
            token_map[symbol[:-len('-EQ')]] = str(token)
        elif symbol.endswith('-BE'):
            be_only[symbol[:-len('-BE')]] = str(token)

    for ticker, token in be_only.items():
        token_map.setdefault(ticker, token)

    token_map['NIFTY_50'] = NIFTY_50_TOKEN
    return token_map


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    try:
        scrip_master = download_scrip_master()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download Angel One scrip master: {e}")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"Scrip master response was not valid JSON: {e}")
        sys.exit(1)

    token_map = build_token_map(scrip_master)
    # 1 because NIFTY_50 is always added manually; anything <=1 means the NSE/-EQ
    # filter matched nothing, which points at a schema change upstream.
    if len(token_map) <= 1:
        logger.error("No NSE equity symbols found in scrip master response; aborting write.")
        sys.exit(1)

    try:
        with open(OUTPUT_PATH, 'w') as f:
            json.dump(token_map, f, indent=2, sort_keys=True)
    except OSError as e:
        logger.error(f"Failed to write '{OUTPUT_PATH}': {e}")
        sys.exit(1)

    logger.info(f"Saved {len(token_map)} symbol -> token mappings to '{OUTPUT_PATH}'.")


if __name__ == "__main__":
    main()

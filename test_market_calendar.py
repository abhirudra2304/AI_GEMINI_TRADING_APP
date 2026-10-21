import json
import time
from datetime import date

import market_calendar as mc


def _cal(tmp_path, holidays, age_days=0):
    p = tmp_path / "h.json"
    p.write_text(json.dumps({"fetched_at": time.time() - age_days * 86400, "holidays": holidays}))
    return str(p)


def _no_network():
    raise AssertionError("must not fetch when the cache is fresh")


def test_holiday_weekend_and_trading_day(tmp_path):
    p = _cal(tmp_path, {"2026-10-20": "Dussehra"})
    assert mc.market_status(date(2026, 10, 20), p, _no_network) == (False, "NSE holiday: Dussehra")
    assert mc.market_status(date(2026, 10, 17), p, _no_network) == (False, "weekend")
    assert mc.market_status(date(2026, 10, 21), p, _no_network) == (True, "")


def test_stale_cache_refreshes_and_is_saved(tmp_path):
    p = _cal(tmp_path, {}, age_days=9)
    new = {"fetched_at": time.time(), "holidays": {"2026-11-10": "Diwali-Balipratipada"}}
    assert mc.market_status(date(2026, 11, 10), p, lambda: new)[0] is False
    assert json.load(open(p))["holidays"] == new["holidays"]


def test_fetch_failure_uses_old_cache(tmp_path):
    p = _cal(tmp_path, {"2026-10-20": "Dussehra"}, age_days=9)
    assert mc.market_status(date(2026, 10, 20), p, lambda: None)[0] is False


def test_no_cache_and_no_network_assumes_trading_day(tmp_path):
    assert mc.market_status(date(2026, 10, 20), str(tmp_path / "missing.json"), lambda: None) == (True, "")

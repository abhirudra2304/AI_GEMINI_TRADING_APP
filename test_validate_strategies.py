import numpy as np
import pandas as pd

import validate_strategies as v


def test_delivery_cost_dominated_by_stt():
    c = v.itemised_cost_frac("SWING", 500.0, 500.0)
    assert 0.0021 < c < 0.0027  # ~0.2% STT + stamp + exch + DP on 1L


def test_intraday_cheaper_than_delivery():
    assert v.itemised_cost_frac("INTRADAY", 500.0, 500.0) < v.itemised_cost_frac("BTST", 500.0, 500.0)


def test_btst_uses_delivery_rates():
    assert v.itemised_cost_frac("BTST", 100.0, 101.0) == v.itemised_cost_frac("SWING", 100.0, 101.0)


def test_slippage_tiers():
    assert v.slippage_per_side(200e7) == 0.0005
    assert v.slippage_per_side(20e7) == 0.0020
    assert v.slippage_per_side(1e7) == 0.0040
    assert v.slippage_per_side(None) == v.SLIPPAGE_UNKNOWN


def _nifty(closes):
    d = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes, "_d": d})


def test_regime_uses_only_prior_bars_and_labels():
    up = _nifty(np.linspace(100, 200, 120))
    day = up["_d"].iloc[-1] + pd.Timedelta(days=1)
    assert v.regime_label(up, day) == "BULL"
    assert v.regime_label(_nifty(np.linspace(200, 100, 120)), day) == "BEAR"
    assert v.regime_label(up.iloc[:30], day) == "UNKNOWN"
    # a huge spike ON the signal day must not change the label (no look-ahead)
    spiked = up.copy()
    spiked.loc[spiked.index[-1], "Close"] = 1e6
    assert v.regime_label(spiked, spiked["_d"].iloc[-1]) == v.regime_label(up.iloc[:-1], spiked["_d"].iloc[-1])


def test_stats_basic():
    s = v.stats(pd.Series([1.0, -1.0, 2.0, -1.0]))
    assert s["n"] == 4 and s["win%"] == 50.0 and abs(s["avgR"] - 0.25) < 1e-9
    assert s["maxDD_R"] == -1.0 and not s["reliable"]
    assert v.stats(pd.Series([], dtype=float)) == {"n": 0}

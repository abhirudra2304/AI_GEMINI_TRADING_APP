# Strategy validation - 2026-11-23 20:06

Net of itemised fees + slippage, notional Rs 100,000/trade. R = net return / planned risk (entry-stop). Informational only; not advice. See audit before trusting any number.

## Results by strategy and regime

| Strategy / regime | n | Win% | Avg net R | Expectancy | PF | MaxDD (R) | t-stat |
|---|---|---|---|---|---|---|---|
| BTST / ALL | 2342 | 35.9 | -0.143 | -14.3% of risk | 0.52 | -336.0 | -12.16 |
| BTST / BULL | 654 | 31.0 | -0.205 | -20.5% of risk | 0.37 | -137.3 | -9.51 |
| BTST / MIXED | 820 | 40.9 | -0.060 | -6.0% of risk | 0.77 | -73.4 | -2.85 |
| BTST / BEAR | 868 | 34.8 | -0.174 | -17.4% of risk | 0.45 | -152.7 | -9.52 |
| BTST / UNKNOWN | 0 | - | - | - | - | - | - |
| INTRADAY / ALL | 69 | 34.8 | -0.065 | -6.5% of risk | 0.69 | -5.9 | -1.08 |
| INTRADAY / BULL | 69 | 34.8 | -0.065 | -6.5% of risk | 0.69 | -5.9 | -1.08 |
| INTRADAY / MIXED | 0 | - | - | - | - | - | - |
| INTRADAY / BEAR | 0 | - | - | - | - | - | - |
| INTRADAY / UNKNOWN | 0 | - | - | - | - | - | - |
| GAP / ALL (n<30 UNRELIABLE) | 10 | 40.0 | -0.013 | -1.3% of risk | 0.92 | -1.2 | -0.10 |
| GAP / BULL (n<30 UNRELIABLE) | 10 | 40.0 | -0.013 | -1.3% of risk | 0.92 | -1.2 | -0.10 |
| GAP / MIXED | 0 | - | - | - | - | - | - |
| GAP / BEAR | 0 | - | - | - | - | - | - |
| GAP / UNKNOWN | 0 | - | - | - | - | - | - |
| SWING / ALL | 6379 | 44.4 | -0.042 | -4.2% of risk | 0.90 | -512.1 | -3.55 |
| SWING / BULL | 154 | 39.0 | -0.136 | -13.6% of risk | 0.64 | -21.0 | -2.07 |
| SWING / MIXED | 1590 | 39.7 | -0.138 | -13.8% of risk | 0.72 | -224.0 | -5.82 |
| SWING / BEAR | 4635 | 46.2 | -0.005 | -0.5% of risk | 0.99 | -478.8 | -0.40 |
| SWING / UNKNOWN | 0 | - | - | - | - | - | - |
| EMFB / ALL | 3308 | 31.0 | -0.253 | -25.3% of risk | 0.63 | -1197.8 | -11.95 |
| EMFB / BULL | 887 | 32.8 | -0.228 | -22.8% of risk | 0.62 | -205.7 | -6.06 |
| EMFB / MIXED | 254 | 24.4 | -0.467 | -46.7% of risk | 0.38 | -121.0 | -6.99 |
| EMFB / BEAR | 2167 | 31.0 | -0.239 | -23.9% of risk | 0.66 | -913.3 | -8.73 |
| EMFB / UNKNOWN | 0 | - | - | - | - | - | - |

## Cost impact vs backtest.py's flat cost

| Strategy | n | Gross avg % | Flat-cost net % (backtest.py) | Itemised+slippage net % | Avg fees % | Avg slippage % |
|---|---|---|---|---|---|---|
| BTST | 2342 | -0.141 | -0.291 | -0.522 | 0.238 | 0.143 |
| EMFB | 3308 | -0.590 | -0.840 | -1.003 | 0.238 | 0.176 |
| GAP | 10 | +0.066 | -0.084 | -0.128 | 0.084 | 0.110 |
| INTRADAY | 69 | +0.043 | -0.107 | -0.162 | 0.083 | 0.122 |
| SWING | 6379 | +0.444 | +0.194 | +0.051 | 0.239 | 0.154 |

## Bias and reliability audit

- **Sample window**: 2026-05-14 to 2026-11-19, 111 distinct signal days (~5 months). One market cycle at most; regime split is thin. Regime mix of trades: {'BEAR': 63.0, 'MIXED': 22.0, 'BULL': 15.0}.
- **Clustering / effective sample size**: avg signals per day per strategy {'BTST': 24.9, 'EMFB': 127.2, 'GAP': 10.0, 'INTRADAY': 11.5, 'SWING': 74.2}. Trades on the same day are highly correlated (same market move), so effective n is closer to the number of days than to the trade count; t-stats here are OVERSTATED.
- **Look-ahead (entry vs signal time)**: 0 trades have entry_time <= signal time (expect 0). Entries are the NEXT candle open after the signal; stops/targets are re-anchored to the actual fill; same-candle stop/target ties resolve to STOP. Regime tag uses Nifty bars strictly before the signal day. Signal-generation look-ahead (indicators computed on a bar that includes data after the signal timestamp, e.g. mid-session daily bars) is NOT excluded by this script: the provisional-daily-bar bug (fixed 2026-09-10) affected cached daily closes before that date.
- **Survivorship / universe**: signals come from today's TARGET_UNIVERSE (222 names, hand-curated and swapped over time, 3-for-3 swap 2026-08-28; +6 recent IPOs 2026-09-25, +ADANIPORTS/MINDACORP/RELIANCE etc. in November). The universe was chosen with hindsight (memory: config-215 hindsight bias), and delisted/collapsed names cannot appear. Results are biased UPWARD.
- **Corporate actions**: backtest.py states it does not adjust for splits/bonuses (known critical flaw). Any symbol with an action inside the replay window can show a phantom gap loss or gain.
- **Overfitting / parameter search**: stops/targets (ATR 1.5-2.0x, R:R 1.5-2.0), thresholds and universe changes were tuned over the same months now used to test them. No holdout, no walk-forward in this script. Treat any positive number as in-sample.
- **Costs**: itemised fees use assumed discount-broker rates (verify). Slippage is a tier assumption by 20d traded value, not measured fills. Entry at next 15-min candle OPEN is optimistic for fast movers; BTST exit is a close-of-next-session time stop, so exit slippage on illiquid names is likely understated. Position impact beyond Rs 1 lakh is ignored.
- **Strategy coverage**: GAP and INTRADAY have a handful of days of signals in the DB; EMFB is rebuilt from saved reports (post-close 15:45+ timestamps, enters next session) and is not what the live system persisted. Small n marks them UNRELIABLE.
- **Regime definition**: Nifty close vs EMA50 and EMA20 vs EMA50 (point-in-time; the Nifty cache has only ~300 bars so EMA200 was unusable; UNKNOWN = under 60 prior bars). `MarketContextEngine` (breadth/VIX/liquidity) cannot be reproduced historically because breadth/AD snapshots are not stored per day, so it is NOT used here.
- **Prior evidence**: memory records no bias-free edge in walk-forward (324 sessions), SWING score inversely related to outcomes (r=-0.052), BTST negative in every month. This report should be read against that, not as new evidence of an edge.

## Reliability verdict

- Not reliable for deployment: single short window, hindsight universe, unadjusted corporate actions, correlated same-day trades, no holdout.
- Rows marked n<30 carry no inferential weight.

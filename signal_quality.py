"""Empirical actionability: is a signal's Confidence tier actually proven,
or just labeled?

scanner_engine.py's Confidence field ('High'/'Medium'/'Low') is a static cut
of EMFB_Score (>75/>60/else - see rank_and_score_emfb). The scorecard
(performance_tracker.py) shows those labels are NOT equally trustworthy -
see CORRECTION below for the current real numbers, which are far weaker
than this module's original 2026-08-07 justification (High 92.3%/12W-1L
across only 13 signals - a tiny, since-regressed sample; do not cite that
figure again).

This module reads the persisted tier win rates (performance_tracker.py's
save_tier_performance/TIER_PERFORMANCE_CACHE_PATH - refreshed by running
`python main.py scorecard`) and re-labels each report row ACTIONABLE /
WATCHLIST_ONLY / SPECULATIVE / INSUFFICIENT_DATA based on what that tier
has actually done, not what its score bucket is named. No changes to
scanner_engine.py or the scoring pipeline - this is a read-after-the-fact
relabeling, same category as trigger_status.py and earnings_verifier.py.

CORRECTION 2026-08-25: the flat DEFAULT_MIN_WIN_RATE=60.0 bar this module
used to apply to every report_type was never reachable once real sample
sizes grew past the tiny original scorecard - EMFB/momentum's actual High
tier win rate is 36.8%/38.5%, so nothing could ever clear 60% and every
row silently landed on SPECULATIVE regardless of tier (confirmed: today's
stockscan shortlist showed SPECULATIVE on every row, including High-
confidence names). The real, meaningful bar is not an arbitrary flat
number - it's whatever win rate the signal's own configured reward:risk
ratio needs to break even (config.EMFB.RR_RATIO=2.5 -> 28.6% breakeven;
config.Scanner.RR_RATIO_BTST/SWING/INTRADAY -> 40%/33.3%/33.3%). A tier
clearing breakeven by a real margin has proven positive expectancy even
at a sub-50% win rate; a tier below breakeven is net value-destroying
even if its win rate looks superficially higher. ACTIONABLE now requires
clearing breakeven + ACTIONABLE_MARGIN_PTS; WATCHLIST_ONLY is the band
between breakeven and that margin (roughly-breakeven, not proven); below
breakeven stays SPECULATIVE. Rows are still never dropped or hidden here
or by any caller - this system's standing rule (see stock_scan.py) is to
label quality, not remove information, so a human still sees everything.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

import config
from performance_tracker import load_tier_performance

logger = logging.getLogger(__name__)

DEFAULT_MIN_SAMPLE_SIZE = 5
ACTIONABLE_MARGIN_PTS = 5.0

# report_type -> the config.py reward:risk ratio that actually governs its
# Target distance. momentum reuses EMFB's scoring/target engine unmodified
# (see momentum_scanner.py's own docstring), so it shares EMFB's ratio.
_RR_RATIO_BY_REPORT_TYPE = {
    'emfb': lambda: config.EMFB.RR_RATIO,
    'momentum': lambda: config.EMFB.RR_RATIO,
    'btst': lambda: config.Scanner.RR_RATIO_BTST,
    'swing': lambda: config.Scanner.RR_RATIO_SWING,
    'intraday': lambda: config.Scanner.RR_RATIO_INTRADAY,
    'gap': lambda: config.Scanner.RR_RATIO_SWING,
}
_DEFAULT_RR_RATIO = 2.0  # matches RR_RATIO_SWING/INTRADAY if report_type is unrecognized


def breakeven_win_rate_pct(report_type: str) -> float:
    """The win rate this report_type's configured reward:risk ratio needs
    to break even, as a percentage: 100 / (1 + RR)."""
    getter = _RR_RATIO_BY_REPORT_TYPE.get((report_type or '').lower())
    rr = getter() if getter else _DEFAULT_RR_RATIO
    return round(100.0 / (1.0 + rr), 1)

# 2026-08-21: the weekly scorecard-refresh cadence documented throughout
# DAILY_RUNBOOK.md was never actually enforced here - a scorecard could be
# arbitrarily old and annotate_actionability would use it exactly as if
# fresh, with no signal to the caller beyond a logger.warning (easy to miss
# in a scheduled-task log) for the "missing entirely" case, and NOTHING at
# all for the "exists but stale" case. STALE_AFTER_DAYS gives stockscan (and
# any other caller) something to actually check and warn loudly about.
STALE_AFTER_DAYS = 10


def annotate_actionability(
    report_df: pd.DataFrame,
    report_type: str,
    min_win_rate: Optional[float] = None,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
) -> pd.DataFrame:
    """Adds an 'Actionability' column to `report_df` based on each row's
    Confidence tier's actual historical win rate, checked against the
    report_type's own reward:risk-implied breakeven (see
    breakeven_win_rate_pct) rather than one flat number across every
    strategy - a 35% win rate is a real edge at 2.5:1 R:R and a real loser
    at 1:1, so the bar has to match the strategy being judged.
      - ACTIONABLE: tier has >= min_sample_size resolved signals AND win
        rate clears breakeven by ACTIONABLE_MARGIN_PTS or more - a real,
        margin-of-safety-backed positive expectancy, not just "not losing."
      - WATCHLIST_ONLY: tier clears breakeven but by less than the margin -
        plausibly breakeven, not proven enough to call a real edge.
      - SPECULATIVE: tier has enough samples but win rate is below
        breakeven - net value-destroying at this reward:risk ratio.
      - INSUFFICIENT_DATA: tier hasn't resolved enough signals yet to trust
        either way (or no scorecard has been run at all) - this is
        deliberately NOT the same as SPECULATIVE; a small sample near 100%
        isn't proven bad, it's just unproven.
    `min_win_rate`, if passed explicitly, overrides the computed breakeven
    bar entirely (back-compat escape hatch); normally leave it None.
    """
    df = report_df.copy()
    breakeven = min_win_rate if min_win_rate is not None else breakeven_win_rate_pct(report_type)
    actionable_bar = breakeven + ACTIONABLE_MARGIN_PTS
    tier_perf = load_tier_performance(report_type)

    if tier_perf is None:
        logger.warning(
            f"No tier_performance.json entry for '{report_type}' - run "
            f"`python main.py scorecard {report_type}` first. Marking every row INSUFFICIENT_DATA."
        )
        df['Actionability'] = 'INSUFFICIENT_DATA'
        df['Tier_Win_Rate_Pct'] = pd.NA
        df.attrs['scorecard_stale'] = True
        df.attrs['scorecard_missing'] = True
        df.attrs['scorecard_age_days'] = None
        return df

    # Staleness check (2026-08-21) - previously a scorecard could be arbitrarily
    # old and get used exactly as if fresh, with no signal beyond a logger line
    # for the "missing entirely" case above. Loud by design: printed here
    # AND exposed via df.attrs so a caller like stock_scan.py can bring it to
    # the top of its own output instead of it staying buried in logs.
    age_days = None
    is_stale = False
    generated_at = tier_perf.get('generated_at')
    if generated_at:
        try:
            age_days = (datetime.now() - datetime.fromisoformat(generated_at)).total_seconds() / 86400
            is_stale = age_days > STALE_AFTER_DAYS
        except (ValueError, TypeError) as e:
            logger.warning(f"Could not parse scorecard generated_at={generated_at!r}: {e}")
    else:
        is_stale = True  # no timestamp at all - treat as unknown-age, so unproven

    df.attrs['scorecard_stale'] = is_stale
    df.attrs['scorecard_missing'] = False
    df.attrs['scorecard_age_days'] = age_days

    if is_stale:
        age_text = f"{age_days:.1f} days old" if age_days is not None else "age unknown (no timestamp)"
        msg = (
            f"⚠️ SCORECARD STALE: '{report_type}' tier_performance.json is {age_text} "
            f"(refresh weekly with `python main.py scorecard {report_type}`) - "
            f"win-rate-based rankings below may not reflect current tier performance."
        )
        print(msg)
        logger.warning(msg)

    by_confidence = {row['Confidence']: row for row in tier_perf.get('by_confidence', [])}

    def _classify(confidence) -> tuple:
        tier = by_confidence.get(confidence)
        if tier is None or tier.get('Resolved', 0) < min_sample_size:
            return 'INSUFFICIENT_DATA', tier.get('Win_Rate_Pct') if tier else None
        win_rate = tier.get('Win_Rate_Pct')
        if win_rate is None:
            return 'INSUFFICIENT_DATA', None
        if win_rate >= actionable_bar:
            return 'ACTIONABLE', win_rate
        if win_rate >= breakeven:
            return 'WATCHLIST_ONLY', win_rate
        return 'SPECULATIVE', win_rate

    classified = df['Confidence'].map(_classify)
    df['Actionability'] = classified.map(lambda t: t[0])
    df['Tier_Win_Rate_Pct'] = classified.map(lambda t: t[1])
    return df


def print_actionable_report(df: pd.DataFrame, top_n: int = 20) -> None:
    if df.empty or 'Actionability' not in df.columns:
        print("\nNo signals to show.")
        return

    display_cols = [c for c in ['Symbol', 'Sector', 'EMFB_Score', 'Confidence', 'Tier_Win_Rate_Pct',
                                 'Trigger', 'Stop', 'Target', 'Earnings_Risk'] if c in df.columns]

    print("\n" + "=" * 100)
    actionable = df[df['Actionability'] == 'ACTIONABLE']
    print(f"ACTIONABLE ({len(actionable)}) - clears its strategy's reward:risk breakeven with real margin")
    print("=" * 100)
    print(actionable[display_cols].head(top_n).to_string(index=False) if not actionable.empty else "  (none)")

    watchlist = df[df['Actionability'] == 'WATCHLIST_ONLY']
    print(f"\nWATCHLIST_ONLY ({len(watchlist)}) - roughly at breakeven, not proven enough to call a real edge")
    print(watchlist[display_cols].head(top_n).to_string(index=False) if not watchlist.empty else "  (none)")

    speculative = df[df['Actionability'] == 'SPECULATIVE']
    print(f"\nSPECULATIVE ({len(speculative)}) - below breakeven, net value-destroying at this reward:risk ratio")
    print(speculative[display_cols].head(top_n).to_string(index=False) if not speculative.empty else "  (none)")

    insufficient = df[df['Actionability'] == 'INSUFFICIENT_DATA']
    print(f"\nINSUFFICIENT_DATA ({len(insufficient)}) - not enough resolved history yet to trust either way")
    print(insufficient[display_cols].head(top_n).to_string(index=False) if not insufficient.empty else "  (none)")
    print("=" * 100)

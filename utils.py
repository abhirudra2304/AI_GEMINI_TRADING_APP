import subprocess
import sys
import logging
from types import ModuleType
from typing import Optional

import pandas as pd
import importlib
logger = logging.getLogger(__name__)

IMPORT_NAME_MAP = {
    "pandas-ta": "pandas_ta",
    "websocket-client": "websocket",
    "smartapi-python": "SmartApi",
    "google-genai": "google.genai",
    "fpdf2": "fpdf",
}

def install_and_import(package: str, import_name: Optional[str] = None, critical: bool = True) -> Optional[ModuleType]:
    """
    Attempt to install and import a package, returning the module.

    :param package: The name of the package to install from pip (e.g., 'fpdf2').
    :param import_name: The name to use for the import statement (e.g., 'fpdf'). Defaults to package name.
    :param critical: If True, exit the application on installation failure. If False, log an error and continue.
    :return: The imported module, or None if import fails and not critical.
    """
    # FIX: Use explicit control flow to help the type checker understand that
    # the name passed to import_module is always a string, never None.
    effective_import_name = import_name
    if not effective_import_name:  # This handles both None and empty strings
        effective_import_name = IMPORT_NAME_MAP.get(package, package)

    try:
        module = importlib.import_module(effective_import_name)
        return module
    except ImportError:
        print(f"Attempting to install missing dependency: {package}")
        try:
            # Using check_call to show installation output to the user.
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])
            print(f"✅ Successfully installed '{package}'.")
            # Invalidate the import caches so Python can find the new module.
            importlib.invalidate_caches()
            module = importlib.import_module(effective_import_name)
            return module
        except Exception as e:
            message = f"Failed to install dependency '{package}'. Please install it manually: pip install {package}"
            if critical:
                print(f"\n[FATAL ERROR] {message}\nOriginal error: {e}")
                sys.exit(1)
            else:
                logger.error(f"❌ {message}\nOriginal error: {e}")
                return None

def add_decision_scores(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty: return df
    ranked = df.copy()

    # --- UNIFIED SCORING LOGIC ---
    # This logic is taken from backtest.py's analysis, which found that
    # the "hottest" signals (high score, high RS) were often over-extended
    # and performed worse. This new score favors less crowded setups.
    # It is now the single source of truth for both live scanning and backtesting.
    # FIX: The original ranked.get('Score', 0).astype(float) is unsafe. If the 'Score'
    # column is missing, .get() returns the default value (an integer 0), and integers
    # do not have an .astype() method. This new pattern safely handles missing columns.
    score_col = ranked.get('Score')
    score_val = score_col.astype(float).clip(0, 100) if score_col is not None else 0.0

    rs_col = ranked.get('RS_Pctl')
    rs_val = rs_col.astype(float).clip(0, 100) if rs_col is not None else 0.0

    sector_col = ranked.get('Sector_RS')
    sector_val = sector_col.astype(float).clip(0, 100) if sector_col is not None else 0.0

    adx_col = ranked.get('ADX')
    adx_val = adx_col.astype(float).clip(0, 50) if adx_col is not None else 0.0

    score_cool = 100 - score_val
    rs_cool = 100 - rs_val
    # Sector_RS is NOT cooled/inverted, unlike the other three components: the
    # backtest finding behind cooling ("hot" signals were overextended and
    # reverted) was about individual-stock momentum exhaustion, a different
    # mechanism from sector-level rotation, which in practice persists for
    # weeks rather than mean-reverting daily. Inverting it was fading sector
    # leadership instead of following it - fine in a choppy tape, wrong when
    # money is rotating cleanly between sectors (see 2026-07-29 signals.db
    # analysis: Top-5 picks skewed to lower Sector_RS than the candidate pool
    # in 72/83 scan groups).
    sector_val_component = sector_val
    adx_cool = 100 - (adx_val / 50.0 * 100)

    ranked['Decision_Score'] = round(
        (rs_cool * 0.40) + (score_cool * 0.35) + (sector_val_component * 0.15) + (adx_cool * 0.10), 1
    )
    # --- END UNIFIED SCORING ---

    risk_pct = ((ranked['Trigger'] - ranked['Stop']) / ranked['Trigger'] * 100).replace([float('inf'), -float('inf')], 0)

    # FIX: Apply the same safe access pattern for EMA50_Distance.
    ema_dist_col = ranked.get('EMA50_Distance')
    ema_distance = ema_dist_col.astype(float).abs() if ema_dist_col is not None else 0.0

    ranked['Risk_Level'] = 'LOW'

    # FIX: Apply the same safe access pattern for Vol_Ratio.
    vol_ratio_col = ranked.get('Vol_Ratio')
    vol_ratio_val = vol_ratio_col.astype(float) if vol_ratio_col is not None else 0.0
    ranked.loc[(risk_pct > 4.0) | (vol_ratio_val > 3.5), 'Risk_Level'] = 'MEDIUM'

    ranked.loc[(risk_pct > 6.0) | (ema_distance > 10.0), 'Risk_Level'] = 'HIGH'

    ranked['Rank_Reason'] = ranked.apply(
        lambda r: f"less crowded confirmed setup: Score {r.get('Score', 0):.1f}, RS {r.get('RS_Pctl', 0):.1f}, ADX {r.get('ADX', 0):.1f}", axis=1
    )
    if 'BTST_Score' in ranked.columns:
        # New BTST-specific final score, independent of intraday execution.
        # FIX: Apply the same safe access pattern for BTST_Score.
        btst_score_col = ranked.get('BTST_Score')
        btst_score = btst_score_col.astype(float).clip(0, 100) if btst_score_col is not None else 0.0
        ranked['BTST_Final_Score'] = ((ranked['Decision_Score'] * 0.40) + (btst_score * 0.60)).round(1)
    elif 'Execution_Score' in ranked.columns:
        # Legacy path for INTRADAY/GAP strategies.
        # FIX: Apply the same safe access pattern for Execution_Score.
        exec_score_col = ranked.get('Execution_Score')
        execution_score = exec_score_col.astype(float).clip(0, 75) if exec_score_col is not None else 0.0
        ranked['BTST_Final_Score'] = ((ranked['Decision_Score'] * 0.70) + (execution_score * 0.30)).round(1)
    return ranked
import subprocess
import sys
import logging

import pandas as pd
import importlib
logger = logging.getLogger(__name__)

def install_and_import(package, import_name=None, critical=True):
    """
    Attempt to install and import a package, returning the module.

    :param package: The name of the package to install from pip (e.g., 'fpdf2').
    :param import_name: The name to use for the import statement (e.g., 'fpdf'). Defaults to package name.
    :param critical: If True, exit the application on installation failure. If False, log an error and continue.
    :return: The imported module, or None if import fails and not critical.
    """
    import_name = import_name or package
    try:
        module = importlib.import_module(import_name)
        return module
    except ImportError:
        print(f"Attempting to install missing dependency: {package}")
        try:
            # Using check_call to show installation output to the user.
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])
            print(f"✅ Successfully installed '{package}'.")
            # Invalidate the import caches so Python can find the new module.
            importlib.invalidate_caches()
            module = importlib.import_module(import_name)
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
    score_val = ranked.get('Score', 0).astype(float).clip(0, 100)
    rs_val = ranked.get('RS_Pctl', 0).astype(float).clip(0, 100)
    sector_val = ranked.get('Sector_RS', 0).astype(float).clip(0, 100)
    adx_val = ranked.get('ADX', 0).astype(float).clip(0, 50)

    score_cool = 100 - score_val
    rs_cool = 100 - rs_val
    sector_cool = 100 - sector_val
    adx_cool = 100 - (adx_val / 50.0 * 100)

    ranked['Decision_Score'] = (
        (rs_cool * 0.40) + (score_cool * 0.35) + (sector_cool * 0.15) + (adx_cool * 0.10)
    ).round(1)
    # --- END UNIFIED SCORING ---

    risk_pct = ((ranked['Trigger'] - ranked['Stop']) / ranked['Trigger'] * 100).replace([float('inf'), -float('inf')], 0)
    ema_distance = ranked.get('EMA50_Distance', 0).astype(float).abs()
    ranked['Risk_Level'] = 'LOW'
    ranked.loc[(risk_pct > 4.0) | (ranked.get('Vol_Ratio', 0).astype(float) > 3.5), 'Risk_Level'] = 'MEDIUM'
    ranked.loc[(risk_pct > 6.0) | (ema_distance > 10.0), 'Risk_Level'] = 'HIGH'

    ranked['Rank_Reason'] = ranked.apply(
        lambda r: f"less crowded confirmed setup: Score {r.get('Score', 0):.1f}, RS {r.get('RS_Pctl', 0):.1f}, ADX {r.get('ADX', 0):.1f}", axis=1
    )
    if 'Execution_Score' in ranked.columns:
        execution_score = ranked.get('Execution_Score', 0).astype(float).clip(0, 75)
        ranked['BTST_Final_Score'] = ((ranked['Decision_Score'] * 0.70) + (execution_score * 0.30)).round(1)
    return ranked
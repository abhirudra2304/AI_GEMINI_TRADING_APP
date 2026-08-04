"""
Central configuration file for the entire trading application.

This file consolidates all tunable parameters, thresholds, and static data
to make the system easier to manage, tune, and deploy.
"""

from zoneinfo import ZoneInfo

# --- General Application Settings ---
class AppConfig:
    # Number of parallel workers for I/O-bound tasks like API calls.
    # The DataBroker's rate limiter prevents overwhelming the API.
    SAFE_API_WORKERS = 12
    # If True, prints detailed debug information instead of the clean report.
    DEBUG_REPORT = False

MARKET_TZ = ZoneInfo("Asia/Kolkata")

# --- Data Caching ---
class CacheConfig:
    # Time-to-live for the discovery cache in minutes.
    CACHE_REFRESH_MINUTES = 60
    # Base directory for historical candle data.
    HISTORICAL_DATA_DIR = 'historical_data'

# --- Universe & Sector Mappings ---
class Universe:
    # Master list of stocks to be used by the discovery and scanning modules.
    TARGET_UNIVERSE = [
    "AARTIIND", "ABB", "ACE", "ACMESOLAR", "ADANIGREEN", "ADANIPOWER", "AFCONS", "AIAENG",
    "ALKEM", "AMBER", "APARINDS", "APOLLO", "APOLLOHOSP", "ASIANPAINT", "ASTRAMICRO", "AUROPHARMA",
    "AXISBANK", "BAJAJFINSV", "BAJFINANCE", "BDL", "BEML", "BHARATFORG", "BORORENEW", "CGPOWER",
    "CIPLA", "CLEAN", "COCHINSHIP", "COFORGE", "CYIENT", "CYIENTDLM", "DATAPATTNS", "DEEPAKNTR",
    "DIVISLAB", "DIXON", "DMART", "EICHERMOT", "ELECON", "ENGINERSIN", "FINEORG", "FLUOROCHEM",
    "FORTIS", "GRSE", "HCLTECH", "HDFCBANK", "HFCL", "HGINFRA", "HINDUNILVR", "ICICIBANK",
    "IDEAFORGE", "INFY", "INOXWIND", "IRCON", "IRFC", "ITC", "JSWENERGY", "JUBLFOOD",
    "JWL", "JYOTICNC", "KAYNES", "KEI", "KIMS", "KNRCON", "KPIGREEN", "KPIL",
    "KPITTECH", "KSB", "LT", "LUPIN", "M&M", "MANKIND", "MARUTI", "MAXHEALTH",
    "MAZDOCK", "MCDOWELL-N", "MIDHANI", "MTARTECH", "NAVINFLUOR", "NBCC", "NCC", "NESTLEIND",
    "NETWEB", "NH", "NHPC", "NTPC", "OFSS", "PARAS", "PERSISTENT", "PGEL",
    "PIDILITIND", "PIIND", "PNCINFRA", "POLYCAB", "POWERGRID", "POWERINDIA", "PREMEXPLN", "PREMIERENE",
    "RAILTEL", "RELIANCE", "RVNL", "SCHNEIDER", "SIEMENS", "SJVN", "SKFINDIA", "SOLARINDS",
    "SRF", "STLTECH", "SUNPHARMA", "SUZLON", "SYRMA", "TAALTECH", "TANLA", "TATACHEM",
    "TATACOMM", "TATACONSUM", "TATAELXSI", "TECHM", "TEJASNET", "TEXRAIL", "THERMAX", "TIMKEN",
    "TITAGARH", "TITAN", "TORNTPHARM", "TRENT", "ULTRACEMCO", "VBL", "VINATIORGA", "WAAREEENER",
    "ZENTEC", "ZYDUSLIFE", "HAL", "BEL", "UNIMECH", "ETERNAL", "KOTAKBANK", "SBIN",
    "PNBHOUSING", "TATAPOWER", "SGEL", "EXIDEIND", "CUMMINSIND", "SOBHA", "ANANTRAJ", "DRREDDY",
    "VIJAYA", "ROUTE", "BHARTIARTL", "TCS", "BSE", "MCX", "CDSL", "CAMS",
    "IREDA", "CONCOR", "INDHOTEL", "BHEL", "SONACOMS", "PNB", "BANKBARODA", "CANBK",
    "UNIONBANK", "INDIANB", "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL", "NATIONALUM",
    "SAIL", "ONGC", "BPCL", "IOC", "OIL", "GAIL", "TMPV", "TMCV",
    "BAJAJ-AUTO", "HEROMOTOCO", "TVSMOTOR", "ASHOKLEY", "SHREECEM", "AMBUJACEM", "ACC", "DALBHARAT",
    "JKCEMENT", "HDFCLIFE", "SBILIFE", "ICICIPRULI", "ICICIGI", "ANGELONE", "BLS", "DCBBANK",
    "GODFRYPHLP", "HINDCOPPER", "LTTS", "MOSCHIP", "NUVAMA", "PSPPROJECT", "PTCIL", "PWL",
    "SARDAEN", "RECLTD", "PFC", "OLECTRA", "JBMA", "TARIL", "VOLTAMP", "KALYANKJIL",
    "COALINDIA",
]

    # Institutional Sector Mappings
    SECTOR_MAP = {
        'HDFCBANK': 'FINANCE', 'ICICIBANK': 'FINANCE', 'KOTAKBANK': 'FINANCE', 'AXISBANK': 'FINANCE',
        'BAJFINANCE': 'FINANCE', 'BAJAJFINSV': 'FINANCE', 'RELIANCE': 'ENERGY', 'TCS': 'IT', 'INFY': 'IT',
        'LT': 'CAPITAL_GOODS', 'ASIANPAINT': 'CONSUMER', 'TITAN': 'CONSUMER', 'BHARTIARTL': 'TELECOM',
        'ULTRACEMCO': 'CEMENT', 'DMART': 'RETAIL', 'POLYCAB': 'CAPITAL_GOODS', 'ABB': 'CAPITAL_GOODS',
        'SIEMENS': 'CAPITAL_GOODS', 'CUMMINSIND': 'CAPITAL_GOODS', 'SRF': 'CHEMICALS', 'PIDILITIND': 'CHEMICALS',
        'TRENT': 'RETAIL', 'EICHERMOT': 'AUTO', 'MARUTI': 'AUTO', 'M&M': 'AUTO',
        'HAL': 'DEFENCE', 'BEL': 'DEFENCE', 'BDL': 'DEFENCE', 'MAZDOCK': 'DEFENCE', 'COCHINSHIP': 'DEFENCE',
        'GRSE': 'DEFENCE', 'SOLARINDS': 'DEFENCE', 'DATAPATTNS': 'DEFENCE', 'PARAS': 'DEFENCE', 'ZENTEC': 'DEFENCE',
        'ASTRAMICRO': 'DEFENCE', 'APOLLO': 'DEFENCE', 'MTARTECH': 'DEFENCE', 'UNIMECH': 'DEFENCE', 'IDEAFORGE': 'DEFENCE',
        'TAALTECH': 'DEFENCE', 'PREMEXPLN': 'DEFENCE', 'CYIENTDLM': 'DEFENCE', 'BEML': 'DEFENCE', 'MIDHANI': 'DEFENCE',
        'DIXON': 'ELECTRONICS', 'KAYNES': 'ELECTRONICS', 'NETWEB': 'ELECTRONICS', 'SYRMA': 'ELECTRONICS',
        'AMBER': 'ELECTRONICS', 'PGEL': 'ELECTRONICS', 'CYIENT': 'IT', 'TATAELXSI': 'IT', 'PERSISTENT': 'IT',
        'KPITTECH': 'IT', 'COFORGE': 'IT', 'TECHM': 'IT', 'HCLTECH': 'IT', 'OFSS': 'IT', 'TANLA': 'IT',
        'ROUTE': 'IT', 'TEJASNET': 'TELECOM', 'HFCL': 'TELECOM', 'STLTECH': 'TELECOM', 'TATACOMM': 'TELECOM',
        'KEI': 'CAPITAL_GOODS', 'APARINDS': 'CAPITAL_GOODS', 'CGPOWER': 'CAPITAL_GOODS', 'POWERINDIA': 'CAPITAL_GOODS',
        'SCHNEIDER': 'CAPITAL_GOODS', 'BHARATFORG': 'AUTO_ANC', 'AIAENG': 'CAPITAL_GOODS', 'THERMAX': 'CAPITAL_GOODS',
        'KSB': 'CAPITAL_GOODS', 'SKFINDIA': 'AUTO_ANC', 'TIMKEN': 'AUTO_ANC', 'ELECON': 'CAPITAL_GOODS',
        'ACE': 'CAPITAL_GOODS', 'JYOTICNC': 'CAPITAL_GOODS',
        'NTPC': 'POWER', 'POWERGRID': 'POWER', 'TATAPOWER': 'POWER', 'ADANIPOWER': 'POWER', 'ADANIGREEN': 'POWER',
        'JSWENERGY': 'POWER', 'NHPC': 'POWER', 'SJVN': 'POWER', 'INOXWIND': 'POWER', 'WAAREEENER': 'POWER',
        'PREMIERENE': 'POWER', 'SUZLON': 'POWER', 'BORORENEW': 'POWER', 'KPIGREEN': 'POWER', 'ACMESOLAR': 'POWER',
        'RVNL': 'RAILWAYS', 'IRCON': 'RAILWAYS', 'RAILTEL': 'RAILWAYS', 'IRFC': 'RAILWAYS', 'TITAGARH': 'RAILWAYS',
        'JWL': 'RAILWAYS', 'TEXRAIL': 'RAILWAYS', 'NCC': 'INFRA', 'KNRCON': 'INFRA', 'PNCINFRA': 'INFRA',
        'KPIL': 'INFRA', 'AFCONS': 'INFRA', 'NBCC': 'INFRA', 'HGINFRA': 'INFRA', 'ENGINERSIN': 'INFRA',
        'SUNPHARMA': 'PHARMA', 'DRREDDY': 'PHARMA', 'CIPLA': 'PHARMA', 'LUPIN': 'PHARMA', 'DIVISLAB': 'PHARMA',
        'MANKIND': 'PHARMA', 'TORNTPHARM': 'PHARMA', 'AUROPHARMA': 'PHARMA', 'ZYDUSLIFE': 'PHARMA', 'ALKEM': 'PHARMA',
        'MAXHEALTH': 'HEALTHCARE', 'APOLLOHOSP': 'HEALTHCARE', 'NH': 'HEALTHCARE', 'FORTIS': 'HEALTHCARE', 'KIMS': 'HEALTHCARE',
        'VBL': 'CONSUMER', 'HINDUNILVR': 'CONSUMER', 'ITC': 'CONSUMER', 'NESTLEIND': 'CONSUMER',
        'TATACONSUM': 'CONSUMER', 'JUBLFOOD': 'CONSUMER', 'MCDOWELL-N': 'CONSUMER',
        'DEEPAKNTR': 'CHEMICALS', 'PIIND': 'CHEMICALS', 'NAVINFLUOR': 'CHEMICALS', 'AARTIIND': 'CHEMICALS',
        'FINEORG': 'CHEMICALS', 'VINATIORGA': 'CHEMICALS', 'FLUOROCHEM': 'CHEMICALS', 'CLEAN': 'CHEMICALS',
        'TATACHEM': 'CHEMICALS',
        'ETERNAL': 'RETAIL', 'SBIN': 'FINANCE', 'PNBHOUSING': 'FINANCE', 'SGEL': 'POWER',
        'EXIDEIND': 'AUTO_ANC', 'SOBHA': 'REALTY', 'ANANTRAJ': 'REALTY', 'VIJAYA': 'HEALTHCARE',
        'BSE': 'FINANCE', 'MCX': 'FINANCE', 'CDSL': 'FINANCE', 'CAMS': 'FINANCE', 'IREDA': 'FINANCE',
        'CONCOR': 'INFRA', 'INDHOTEL': 'CONSUMER', 'BHEL': 'CAPITAL_GOODS', 'SONACOMS': 'AUTO_ANC',
        # --- PSU BANKS (added) ---
        'PNB': 'PSU_BANK', 'BANKBARODA': 'PSU_BANK', 'CANBK': 'PSU_BANK',
        'UNIONBANK': 'PSU_BANK', 'INDIANB': 'PSU_BANK',
        # --- METALS (added) ---
        'TATASTEEL': 'METAL', 'JSWSTEEL': 'METAL', 'HINDALCO': 'METAL', 'VEDL': 'METAL',
        'JINDALSTEL': 'METAL', 'NATIONALUM': 'METAL', 'SAIL': 'METAL',
        # --- OIL & GAS (added) ---
        'ONGC': 'ENERGY', 'BPCL': 'ENERGY', 'IOC': 'ENERGY', 'OIL': 'ENERGY', 'GAIL': 'ENERGY',
        # --- AUTO (added) ---
        'TMPV': 'AUTO', 'TMCV': 'AUTO', 'BAJAJ-AUTO': 'AUTO', 'HEROMOTOCO': 'AUTO', 'TVSMOTOR': 'AUTO', 'ASHOKLEY': 'AUTO',
        # --- CEMENT (added) ---
        'SHREECEM': 'CEMENT', 'AMBUJACEM': 'CEMENT', 'ACC': 'CEMENT', 'DALBHARAT': 'CEMENT', 'JKCEMENT': 'CEMENT',
        # --- INSURANCE (added, grouped under FINANCE) ---
        'HDFCLIFE': 'FINANCE', 'SBILIFE': 'FINANCE', 'ICICIPRULI': 'FINANCE', 'ICICIGI': 'FINANCE',
        # --- Previously orphaned BETA_REGISTRY-only entries (added to TARGET_UNIVERSE) ---
        # BLS and PWL have no confident sector classification - OTHER (falls back to
        # Nifty 50 for RS benchmarking) rather than a guessed-wrong sector.
        'ANGELONE': 'FINANCE', 'DCBBANK': 'FINANCE', 'NUVAMA': 'FINANCE',
        'GODFRYPHLP': 'CONSUMER', 'HINDCOPPER': 'METAL', 'SARDAEN': 'METAL',
        'LTTS': 'IT', 'MOSCHIP': 'IT', 'PSPPROJECT': 'INFRA', 'PTCIL': 'DEFENCE',
        'BLS': 'OTHER', 'PWL': 'OTHER',
        # --- High-Velocity BTST & Swing Candidates / AI-Semiconductor additions ---
        'RECLTD': 'FINANCE', 'PFC': 'FINANCE', 'COALINDIA': 'ENERGY',
        'OLECTRA': 'AUTO', 'JBMA': 'AUTO_ANC', 'TARIL': 'CAPITAL_GOODS', 'VOLTAMP': 'CAPITAL_GOODS',
        'KALYANKJIL': 'RETAIL', 'AZAD': 'DEFENCE', 'DCXINDIA': 'DEFENCE',
        '360ONE': 'FINANCE', 'MOTILALOFS': 'FINANCE', 'POLICYBZR': 'FINANCE',
        'ADANIPORTS': 'INFRA', 'E2E': 'IT', 'RIR': 'CAPITAL_GOODS',
        'AFFLE': 'IT', 'RATEGAIN': 'IT', 'AVALON': 'ELECTRONICS',
    }

    # Mapping from SECTOR_MAP values to Nifty Index symbols for RS calculations
    SECTOR_INDEX_MAP = {
        'FINANCE': 'NIFTY FINANCIAL SERVICES',
        'IT': 'NIFTY IT',
        'ENERGY': 'NIFTY ENERGY',
        'CONSUMER': 'NIFTY FMCG',
        'CAPITAL_GOODS': 'NIFTY CAPITAL GOODS',
        'CEMENT': 'NIFTY COMMODITIES',
        'RETAIL': 'NIFTY CONSUMER DURABLES',
        'CHEMICALS': 'NIFTY COMMODITIES',
        'AUTO': 'NIFTY AUTO',
        'DEFENCE': 'NIFTY INDIA DEFENCE',
        'ELECTRONICS': 'NIFTY CONSUMER DURABLES',
        'TELECOM': 'NIFTY TELECOM',
        'AUTO_ANC': 'NIFTY AUTO',
        'POWER': 'NIFTY ENERGY',
        'RAILWAYS': 'NIFTY TRANSPORT & LOGISTICS',
        'INFRA': 'NIFTY INFRASTRUCTURE',
        'PHARMA': 'NIFTY PHARMA',
        'HEALTHCARE': 'NIFTY HEALTHCARE INDEX',
        'REALTY': 'NIFTY REALTY',
        'PSU_BANK': 'NIFTY PSU BANK',   # added
        'METAL': 'NIFTY METAL',         # added
        'OTHER': 'Nifty 50' # Fallback for unmapped sectors
    }

    # Stock-specific volatility multipliers for stop-loss calculation.
    BETA_REGISTRY = {
        'DIXON': 1.65, 'TRENT': 1.55, 'COCHINSHIP': 1.60, 'MAZDOCK': 1.45,
        'GRSE': 1.40, 'DATAPATTNS': 1.35, 'ASTRAMICRO': 1.38, 'ZENTEC': 1.30,
        'NETWEB': 1.42, 'KAYNES': 1.35, 'IREDA': 1.50, 'RVNL': 1.55,
        'IRFC': 1.45, 'HINDCOPPER': 1.32, 'BSE': 1.48, 'ANGELONE': 1.40,
        'PERSISTENT': 1.15, 'COFORGE': 1.18, 'NUVAMA': 1.10, 'MCX': 1.05,
        'CDSL': 1.12, 'M&M': 1.02, 'BLS': 1.08, 'SARDAEN': 1.10,
        'PWL': 1.05, 'TATAPOWER': 1.01, 'BDL': 1.22, 'HAL': 1.25,
        'BEL': 1.18, 'PTCIL': 1.12, 'SOLARINDS': 0.95, 'TCS': 0.76,
        'INFY': 0.82, 'GODFRYPHLP': 0.72, 'INDHOTEL': 0.88, 'PSPPROJECT': 0.85,
        'CAMS': 1.15, 'LTTS': 1.20, 'MOSCHIP': 1.50,
        'IDEAFORGE': 1.45, 'CONCOR': 0.90, 'DCBBANK': 0.78, 'STLTECH': 1.40,
        'ETERNAL': 1.45, 'SBIN': 1.10, 'PNBHOUSING': 1.35, 'SGEL': 1.40,
        'EXIDEIND': 1.15, 'SOBHA': 1.50, 'ANANTRAJ': 1.42, 'VIJAYA': 1.05,
        'BHEL': 1.35, 'SONACOMS': 1.20,
        # Computed beta (cov/var of daily returns vs Nifty 50, ~1yr local cache) for the
        # remaining TARGET_UNIVERSE tickers.
        # TAALTECH: only ~63 days of history since IPO (recent listing) - the raw
        # regression beta (0.58) is misleading because its correlation to Nifty 50 is
        # weak while its own volatility is ~4.6x the market's. Using a volatility-ratio
        # proxy (stock_std/market_std) capped at the registry's observed max instead,
        # so its stop-loss multiplier isn't tightened for a name that swings this hard.
        # Revisit with a proper correlation beta once it has a longer trading history.
        'TAALTECH': 1.8,
        'AARTIIND': 1.4, 'ABB': 0.94, 'ACE': 1.07, 'ACMESOLAR': 0.33, 'ADANIGREEN': 1.67,
        'ADANIPOWER': 1.07, 'AFCONS': 0.66, 'AIAENG': 0.69, 'ALKEM': 0.65, 'AMBER': 1.33,
        'APARINDS': 1.36, 'APOLLO': 1.64, 'APOLLOHOSP': 0.65, 'ASIANPAINT': 0.98, 'AUROPHARMA': 0.49,
        'AXISBANK': 1.16, 'BAJAJFINSV': 1.27, 'BAJFINANCE': 1.46, 'BEML': 1.52, 'BHARATFORG': 1.22,
        'BHARTIARTL': 0.69, 'BORORENEW': 1.33, 'CGPOWER': 1.08, 'CIPLA': 0.58, 'CLEAN': 1.05,
        'CUMMINSIND': 0.92, 'CYIENT': 1.19, 'CYIENTDLM': 1.27, 'DEEPAKNTR': 0.95, 'DIVISLAB': 0.47,
        'DMART': 0.61, 'DRREDDY': 0.51, 'EICHERMOT': 1.24, 'ELECON': 1.46, 'ENGINERSIN': 1.34,
        'FINEORG': 0.89, 'FLUOROCHEM': 0.6, 'FORTIS': 0.77, 'HCLTECH': 0.91, 'HDFCBANK': 1.21,
        'HFCL': 1.59, 'HGINFRA': 1.44, 'HINDUNILVR': 0.66, 'ICICIBANK': 0.94, 'INOXWIND': 1.76,
        'IRCON': 1.76, 'ITC': 0.66, 'JSWENERGY': 1.06, 'JUBLFOOD': 1.1, 'JWL': 1.51,
        'JYOTICNC': 1.4, 'KEI': 1.17, 'KIMS': 0.71, 'KNRCON': 1.12, 'KOTAKBANK': 0.99,
        'KPIGREEN': 1.44, 'KPIL': 0.84, 'KPITTECH': 1.05, 'KSB': 0.57, 'LT': 1.33,
        'LUPIN': 0.53, 'MANKIND': 0.65, 'MARUTI': 1.09, 'MAXHEALTH': 0.74, 'MCDOWELL-N': 0.77,
        'MIDHANI': 1.46, 'MTARTECH': 1.28, 'NAVINFLUOR': 0.67, 'NBCC': 1.64, 'NCC': 1.31,
        'NESTLEIND': 0.67, 'NH': 0.74, 'NHPC': 0.9, 'NTPC': 0.63, 'OFSS': 0.94,
        'PARAS': 1.17, 'PGEL': 1.83, 'PIDILITIND': 0.97, 'PIIND': 0.92, 'PNCINFRA': 1.07,
        'POLYCAB': 1.08, 'POWERGRID': 0.66, 'POWERINDIA': 0.65, 'PREMEXPLN': 1.16, 'PREMIERENE': 0.88,
        'RAILTEL': 1.73, 'RELIANCE': 0.99, 'ROUTE': 1.09, 'SCHNEIDER': 1.04, 'SIEMENS': 1.04,
        'SJVN': 1.26, 'SKFINDIA': 0.53, 'SRF': 1.21, 'SUNPHARMA': 0.42, 'SUZLON': 1.32,
        'SYRMA': 1.36, 'TANLA': 1.23, 'TATACHEM': 0.78, 'TATACOMM': 0.85, 'TATACONSUM': 0.68,
        'TATAELXSI': 1.08, 'TECHM': 0.7, 'TEJASNET': 1.22, 'TEXRAIL': 1.91, 'THERMAX': 0.41,
        'TIMKEN': 0.71, 'TITAGARH': 1.54, 'TITAN': 0.9, 'TORNTPHARM': 0.33, 'ULTRACEMCO': 1.28,
        'UNIMECH': 1.13, 'VBL': 1.04, 'VINATIORGA': 0.74, 'WAAREEENER': 1.01, 'ZYDUSLIFE': 0.65,
        # --- PSU BANKS (added, estimates - refine with regression like the rest of this registry) ---
        'PNB': 1.55, 'BANKBARODA': 1.35, 'CANBK': 1.40, 'UNIONBANK': 1.45, 'INDIANB': 1.35,
        # --- METALS (added, estimates) ---
        'TATASTEEL': 1.15, 'JSWSTEEL': 1.10, 'HINDALCO': 1.25, 'VEDL': 1.45,
        'JINDALSTEL': 1.35, 'NATIONALUM': 1.30, 'SAIL': 1.40,
        # --- OIL & GAS (added, estimates) ---
        'ONGC': 1.05, 'BPCL': 1.10, 'IOC': 1.05, 'OIL': 1.15, 'GAIL': 0.95,
        # --- AUTO (added, estimates) ---
        # TMPV/TMCV: live-computed (cov/var vs Nifty 50) post-demerger, not estimates.
        'TMPV': 1.53, 'TMCV': 2.04, 'BAJAJ-AUTO': 0.85, 'HEROMOTOCO': 0.90, 'TVSMOTOR': 1.10, 'ASHOKLEY': 1.30,
        # --- CEMENT (added, estimates) ---
        'SHREECEM': 0.80, 'AMBUJACEM': 0.90, 'ACC': 0.88, 'DALBHARAT': 1.05, 'JKCEMENT': 0.95,
        # --- INSURANCE (added, estimates) ---
        'HDFCLIFE': 0.75, 'SBILIFE': 0.80, 'ICICIPRULI': 0.85, 'ICICIGI': 0.70,
        # --- High-Velocity BTST & Swing / AI-Semiconductor additions ---
        # Live-computed (cov/var vs Nifty 50) except RIR and RATEGAIN, which hit
        # a live-data gap (3 trading days / API failure respectively) and fell
        # through to the generic 1.0 default rather than a genuine regression -
        # revisit both once they have usable candle history.
        'RECLTD': 1.14, 'PFC': 0.94, 'OLECTRA': 1.18, 'JBMA': 0.95, 'TARIL': 1.41,
        'VOLTAMP': 0.66, 'KALYANKJIL': 1.68, 'COALINDIA': 0.5, 'AZAD': 0.83,
        'DCXINDIA': 1.76, '360ONE': 1.32, 'MOTILALOFS': 2.15, 'ADANIPORTS': 1.21,
        'E2E': 1.07, 'RIR': 1.0, 'AFFLE': 0.5, 'RATEGAIN': 1.0, 'AVALON': 0.92,
        'POLICYBZR': 0.88,
    }

# --- Discovery Phase Settings ---
class Discovery:
    # Lookback window for calculating returns for different strategies.
    LOOKBACK_SWING = 90
    LOOKBACK_BTST = 14
    # Minimum average traded value (in Crores) for a stock to be considered liquid.
    LIQUIDITY_THRESHOLD_CRORES = 10
    # Minimum Relative Strength percentile for a stock to pass the initial filter.
    RS_PCT_THRESHOLD = 40.0
    # Minimum RSI value for a stock to pass the initial filter.
    RSI_THRESHOLD = 50.0
    # Daily volume ratio to identify a potential volume shock.
    VOL_SHOCK_RATIO_DAILY = 2.5
    # Number of top candidates to pass from discovery to the confirmation scan.
    TOP_N_WATCHLIST = 10
    # Number of top candidates to use for the fast execution scan.
    TOP_N_FAST_SCAN = 20
    # Minimum fraction of the scanned universe that must share the most
    # recent fetched trading day's close for a discovery run to be trusted.
    # Below this, execute_macro_discovery only warns (doesn't abort - this
    # feeds the live/interactive scanners, not just a batch job) that a
    # large chunk of the universe is scoring off stale cached data.
    MIN_FRESH_DATA_FRACTION = 0.5
    # Width of the confirmed-signal window tracked for top-N drop-reason
    # logging (see orchestrator.py/cache.py's topn_history) - how many of the
    # current run's top-ranked signals get persisted and diffed against the
    # next same-strategy run.
    TOP_N_TRACKED_FOR_DIFF = 10

# --- Scanner Engine Settings ---
class Scanner:
    # Base ATR multiplier for stop loss calculation.
    ATR_MULTIPLIER_SWING = 2.0
    ATR_MULTIPLIER_BTST = 1.5
    ATR_MULTIPLIER_INTRADAY = 1.5
    # Risk-to-Reward ratio for target price calculation.
    RR_RATIO_SWING = 2.0
    RR_RATIO_BTST = 1.5
    RR_RATIO_INTRADAY = 2.0

# --- Execution Score Thresholds ---
class ExecutionThresholds:
    # Minimum score to be considered for a BUY TODAY recommendation.
    BUY_TODAY_SCORE = 55
    # Minimum score to be considered for a WATCH recommendation.
    WATCH_SCORE = 42

# --- Live Streaming Settings ---
class Live:
    # Minimum last traded quantity to be considered a "volume spike".
    VOLUME_SPIKE_THRESHOLD_SHARES = 25000
    # Minimum single-print traded value (in rupees) to be considered a block deal spike.
    VOLUME_SPIKE_THRESHOLD_VALUE = 2_500_000
    # Cooldown period in seconds to prevent re-scanning the same stock too frequently.
    DEBOUNCE_SECONDS = 60

# --- Continuous Live Scanner Settings ---
class LiveScanner:
    # Interval in seconds to re-run the fast execution scan.
    LOOP_INTERVAL_SECONDS = 60
    # Interval in minutes to re-run the broad discovery scan.
    DISCOVERY_REFRESH_MINUTES = 30

# --- Backtesting Settings ---
class Backtest:
    # Number of 15-minute candles in a typical NSE trading session.
    SESSION_CANDLES_15M = 26
    # Default holding periods in candles for different strategies.
    HOLDING_PERIOD_CANDLES = {
        "BTST": SESSION_CANDLES_15M,
        "GAP": SESSION_CANDLES_15M,
        "SWING": SESSION_CANDLES_15M * 5,
        "INTRADAY": SESSION_CANDLES_15M,
        "EMFB": SESSION_CANDLES_15M * 10,
    }
    # Estimated all-in cost (brokerage, STT, etc.) as a decimal.
    DELIVERY_COST_PCT = 0.0025
    INTRADAY_COST_PCT = 0.0015

# --- EMFB (Emerging Momentum / Fresh Breakout) ---
class EMFB_Regime:
    """Activation thresholds for the EMFB scanner."""
    # Run EMFB if Nifty 50 daily return is below this value.
    NIFTY_RETURN_THRESHOLD = -0.8
    # Run EMFB if Bank Nifty daily return is below this value.
    BANKNIFTY_RETURN_THRESHOLD = -1.0
    # Run EMFB if Midcap 100 daily return is below this value.
    MIDCAP_RETURN_THRESHOLD = -1.2
    # Run EMFB if Smallcap 100 daily return is below this value.
    SMALLCAP_RETURN_THRESHOLD = -1.5
    # Run EMFB if Advance/Decline ratio is below this value.
    AD_RATIO_THRESHOLD = 0.6
    # Run EMFB if India VIX is above this value.
    VIX_THRESHOLD = 18.0
    # Set to True to force EMFB to run regardless of market regime.
    FORCE_RUN = False

class EMFB:
    """Scoring weights for the EMFB strategy."""
    # Which index to use for the universe (from data/ folder)
    UNIVERSE_INDEX = "nifty500"
    # Minimum score for a stock to be included in the report.
    MIN_SCORE_THRESHOLD = 45
    # Number of top stocks to display and save in the final report.
    REPORT_TOP_N = 20

    # --- Dynamic Weighting Profiles ---
    # Weights are applied to the percentile rank (0-100) of each metric.
    # The sum of weights for each profile should ideally be 100.
    WEIGHT_PROFILES = {
        # Profile for weak/bearish market days. Focus on survival and recovery.
        'BEARISH_MARKET': {
            'market_survivor_rank': 25,      # Stocks that defied the downturn.
            'recovery_rank': 20,             # Strong bounce from intraday lows.
            'rs_nifty_1d_rank': 15,          # Immediate relative strength is key.
            'last_hour_vol_rank': 10,        # Late session accumulation.
            'closing_strength_rank': 10,     # Finishing strong.
            'vwap_rank': 5,                  # Holding above session VWAP.
            'breakout_rank': 5,              # Breakouts are less reliable here.
            'rs_nifty_5d_rank': 5,           # Medium-term RS.
            'rs_sector_1d_rank': 5,          # Sector context.
        },
        # Profile for neutral or range-bound days. Balanced approach.
        'NEUTRAL_MARKET': {
            'rs_nifty_5d_rank': 20,          # Medium-term RS is more reliable.
            'breakout_rank': 15,             # Quality setups matter.
            'market_survivor_rank': 15,
            'recovery_rank': 10,
            'last_hour_vol_rank': 10,
            'closing_strength_rank': 10,
            'rs_nifty_1d_rank': 5,
            'vwap_rank': 10,
            'rs_sector_1d_rank': 5,
        },
        # Default/fallback profile.
        'DEFAULT': {
            'rs_nifty_5d_rank': 20,
            'breakout_rank': 15,
            'market_survivor_rank': 15,
            'recovery_rank': 10,
            'last_hour_vol_rank': 10,
            'closing_strength_rank': 10,
            'rs_nifty_1d_rank': 5,
            'vwap_rank': 10,
            'rs_sector_1d_rank': 5,
        }
    }

    # Minimum Risk:Reward for target calculation
    RR_RATIO = 2.5

# --- Analysis & Recommendation Logic ---
class HoldingRecommendation:
    """Thresholds for the 'analyze' command's holding recommendation."""
    # Minimum liquidity (in rupees) for a stock to be considered holdable.
    MIN_LIQUIDITY_RUPEES = 100_000_000
    # Minimum RS percentile to be considered holdable.
    MIN_RS_PERCENTILE = 40.0
    # Minimum Decision Score to recommend HOLD over WATCH.
    MIN_DECISION_SCORE_FOR_HOLD = 65.0
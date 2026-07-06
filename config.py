"""
Central configuration file for the entire trading application.

This file consolidates all tunable parameters, thresholds, and static data
to make the system easier to manage, tune, and deploy.
"""

# --- General Application Settings ---
class AppConfig:
    # Number of parallel workers for I/O-bound tasks like API calls.
    # The DataBroker's rate limiter prevents overwhelming the API.
    SAFE_API_WORKERS = 16
    # If True, prints detailed debug information instead of the clean report.
    DEBUG_REPORT = False

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
        "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "CHOLAFIN", "AXISBANK", "HDFCBANK", "ICICIBANK", "SBIN",
        "KOTAKBANK", "INDUSINDBK", "FEDERALBNK", "IDFCFIRSTB", "BANDHANBNK", "CANBK", "PNB", "BANKBARODA",
        "UNIONBANK", "INDIANB", "INDBANK", "UCOBANK", "BAJFINANCE", "BAJAJFINSV", "SHRIRAMFIN", "AUBANK",
        "MUTHOOTFIN", "MANAPPURAM", "PFC", "RECLTD", "360ONE", "ANGELONE", "MOTILALOFS", "MCX",
        "BSE", "CDSL", "CAMS", "KFINTECH", "CREDITACC", "TCS", "INFY", "HCLTECH",
        "WIPRO", "TECHM", "LTIM", "PERSISTENT", "COFORGE", "MPHASIS", "LTTS", "OFSS",
        "CYIENT", "KPITTECH", "TATAELXSI", "NEWGEN", "SONATSOFTW", "BHARTIARTL", "RELIANCE", "TATACOMM",
        "HFCL", "TEJASNET", "HAL", "BEL", "BDL", "MAZDOCK", "COCHINSHIP", "GRSE",
        "SOLARINDS", "DATAPATTERNS", "PARAS", "ASTRAMICRO", "ZENTEC", "IDEAFORGE", "LT", "SIEMENS",
        "ABB", "CGPOWER", "CUMMINSIND", "KAYNES", "DIXON", "POLYCAB", "KEI", "APLAPOLLO",
        "SCHNEIDER", "HAVELLS", "VOLTAS", "BLUESTARCO", "HITACHIENER", "SKFINDIA", "THERMAX", "CARBORUNIV",
        "M", "JSWINFRA", "APARINDS", "KPRMILL", "WELSPUNLIV", "PINELABS", "TRIDENT", "FIRSTCRY",
        "RAMCOCEM", "EXIDEIND", "JYOTICNC", "INDIGO", "SHREECEM", "HINDPETRO", "TRENT", "ADANIENSOL",
        "BAJAJ-AUTO", "NTPC", "SBILIFE", "IDEA", "YESBANK", "VEDL", "VTL", "PAGEIND",
        "GOKEX", "ICIL", "ARVIND", "MANYAVAR", "PGIL", "ZOMATO", "ASIANPAINT", "TITAN",
        "ULTRACEMCO", "DMART", "SRF", "PIDILITIND", "EICHERMOT", "MARUTI", "M&M", "DATAPATTNS",
        "APOLLO", "MTARTECH", "UNIMECH", "TAALENT", "PREMEXPLN", "CYIENTDLM", "BEML", "MIDHANI",
        "NETWEB", "SYRMA", "AMBER", "PGEL", "TANLA", "ROUTE", "STLTECH", "POWERINDIA",
        "BHARATFORG", "AIAENG", "KSB", "TIMKEN", "ELECON", "ACE", "LAXMIMACH", "POWERGRID",
        "TATAPOWER", "ADANIPOWER", "ADANIGREEN", "JSWENERGY", "NHPC", "SJVN", "INOXWIND", "WAAREEENER",
        "PREMIERENE", "SUZLON", "BORORENEW", "KPIGREEN", "ACMESOLAR", "RVNL", "IRCON", "RAILTEL",
        "IRFC", "TITAGARH", "JWL", "TEXRAIL", "NCC", "KNRCON", "PNCINFRA", "KPIL",
        "AFCONS", "NBCC", "HGINFRA", "ENGINERSIN", "SUNPHARMA", "DRREDDY", "CIPLA", "LUPIN",
        "DIVISLAB", "MANKIND", "TORNTPHARM", "AUROPHARMA", "ZYDUSLIFE", "ALKEM", "MAXHEALTH", "NH",
        "FORTIS", "KIMS", "VBL", "HINDUNILVR", "ITC", "NESTLEIND", "TATACONSUM", "JUBLFOOD",
        "MCDOWELL-N", "DEEPAKNTR", "PIIND", "NAVINFLUOR", "AARTIIND", "FINEORG", "VINATIORGA", "FLUOROCHEM",
        "CLEAN", "TATACHEM",
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
        'TAALENT': 'DEFENCE', 'PREMEXPLN': 'DEFENCE', 'CYIENTDLM': 'DEFENCE', 'BEML': 'DEFENCE', 'MIDHANI': 'DEFENCE',
        'DIXON': 'ELECTRONICS', 'KAYNES': 'ELECTRONICS', 'NETWEB': 'ELECTRONICS', 'SYRMA': 'ELECTRONICS',
        'AMBER': 'ELECTRONICS', 'PGEL': 'ELECTRONICS', 'CYIENT': 'IT', 'TATAELXSI': 'IT', 'PERSISTENT': 'IT',
        'KPITTECH': 'IT', 'COFORGE': 'IT', 'TECHM': 'IT', 'HCLTECH': 'IT', 'OFSS': 'IT', 'TANLA': 'IT',
        'ROUTE': 'IT', 'TEJASNET': 'TELECOM', 'HFCL': 'TELECOM', 'STLTECH': 'TELECOM', 'TATACOMM': 'TELECOM',
        'KEI': 'CAPITAL_GOODS', 'APARINDS': 'CAPITAL_GOODS', 'CGPOWER': 'CAPITAL_GOODS', 'POWERINDIA': 'CAPITAL_GOODS',
        'SCHNEIDER': 'CAPITAL_GOODS', 'BHARATFORG': 'AUTO_ANC', 'AIAENG': 'CAPITAL_GOODS', 'THERMAX': 'CAPITAL_GOODS',
        'KSB': 'CAPITAL_GOODS', 'SKFINDIA': 'AUTO_ANC', 'TIMKEN': 'AUTO_ANC', 'ELECON': 'CAPITAL_GOODS',
        'ACE': 'CAPITAL_GOODS', 'JYOTICNC': 'CAPITAL_GOODS', 'LAXMIMACH': 'CAPITAL_GOODS',
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
        'TATACHEM': 'CHEMICALS'
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
        'IDEAFORGE': 1.45, 'CONCOR': 0.90, 'DCBBANK': 0.78, 'STLTECH': 1.40
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

# --- Scanner Engine Settings ---
class Scanner:
    # Base ATR multiplier for stop loss calculation.
    ATR_MULTIPLIER_SWING = 2.0
    ATR_MULTIPLIER_BTST = 1.5
    # Risk-to-Reward ratio for target price calculation.
    RR_RATIO_SWING = 2.0
    RR_RATIO_BTST = 1.5

# --- Live Streaming Settings ---
class Live:
    # Minimum last traded quantity to be considered a "volume spike".
    VOLUME_SPIKE_THRESHOLD_SHARES = 25000
    # Cooldown period in seconds to prevent re-scanning the same stock too frequently.
    DEBOUNCE_SECONDS = 60

# --- Backtesting Settings ---
class Backtest:
    # Number of 15-minute candles in a typical NSE trading session.
    SESSION_CANDLES_15M = 26
    # Default holding periods in candles for different strategies.
    HOLDING_PERIOD_CANDLES = {
        "BTST": SESSION_CANDLES_15M,
        "GAP": SESSION_CANDLES_15M,
        "SWING": SESSION_CANDLES_15M * 5,
    }
    # Estimated all-in cost (brokerage, STT, etc.) as a decimal.
    DELIVERY_COST_PCT = 0.0025
    INTRADAY_COST_PCT = 0.0015
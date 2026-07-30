import os
import pickle
import logging
from datetime import datetime, timedelta
import pandas as pd
from typing import Optional, Tuple, Dict, Any, List

import config
from lifecycle_manager import shutdown_manager

logger = logging.getLogger(__name__)

DEFAULT_DISCOVERY_CACHE_FILE = "discovery_cache.pkl"
STRATEGY_DISCOVERY_CACHE_FILES = {
    "BTST": "discovery_cache_btst.pkl",
    "SWING": "discovery_cache_swing.pkl",
    "GAP": "discovery_cache_gap.pkl",
    "INTRADAY": "discovery_cache_intraday.pkl",
}
DEFAULT_LAST_SIGNALS_FILE = "last_signals.pkl"

class DiscoveryCache:
    """Owns the Phase 1 discovery snapshot so Phase 2 never refetches daily data."""
    _memory_cache = {}

    def __init__(self, path: Optional[str] = None, ttl_minutes: int = config.CacheConfig.CACHE_REFRESH_MINUTES):
        self._explicit_path = path
        self.path = path or DEFAULT_DISCOVERY_CACHE_FILE
        self.ttl = timedelta(minutes=ttl_minutes)
        self._last_load_error = None
        shutdown_manager.register(self.save_on_shutdown)

    def save_on_shutdown(self):
        """No-op: save_cache() already writes to disk synchronously, so there is nothing to flush here."""
        pass


    @staticmethod
    def _normalize_strategy(strategy: Optional[str] = None) -> Optional[str]:
        return strategy.upper() if strategy else None

    def _cache_path_for_strategy(self, strategy: Optional[str] = None) -> str:
        if self._explicit_path:
            return self._explicit_path
        normalized = self._normalize_strategy(strategy)
        # FIX: Check if normalized is None before asking the dictionary
        if not normalized:
            return DEFAULT_DISCOVERY_CACHE_FILE
        return STRATEGY_DISCOVERY_CACHE_FILES.get(normalized, DEFAULT_DISCOVERY_CACHE_FILE)

    def _load_path_for_strategy(self, strategy: Optional[str] = None) -> str:
        primary_path = self._cache_path_for_strategy(strategy)
        if self._explicit_path or os.path.exists(primary_path):
            return primary_path

        # Backward compatibility: accept the old shared cache if no strategy file exists yet.
        normalized = self._normalize_strategy(strategy)
        if normalized in STRATEGY_DISCOVERY_CACHE_FILES and os.path.exists(DEFAULT_DISCOVERY_CACHE_FILE):
            return DEFAULT_DISCOVERY_CACHE_FILE
        return primary_path

    @staticmethod
    def _is_market_hours(value: datetime) -> bool:
        market_time = value.astimezone(config.MARKET_TZ).time()
        return datetime.strptime("09:15", "%H:%M").time() <= market_time <= datetime.strptime("15:30", "%H:%M").time()

    def load_cache(self, strategy: Optional[str] = None) -> Optional[Dict[str, Any]]:
        self._last_load_error = None
        path = self._load_path_for_strategy(strategy)
        self.path = path
        if not os.path.exists(path):
            self._memory_cache.pop(path, None)
            return None
        cached = self._memory_cache.get(path)
        if cached is not None:
            return cached
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
            self._memory_cache[path] = payload
            return payload
        except Exception as e:
            self._last_load_error = str(e)
            logger.warning(f"Failed to load discovery cache: {e}")
            return None

    def save_cache(self, discovered_df: pd.DataFrame, watchlist: List[str], strategy: str, regime: Dict[str, Any]) -> Dict[str, Any]:
        normalized_strategy = self._normalize_strategy(strategy) or "SWING"
        path = self._cache_path_for_strategy(normalized_strategy)
        self.path = path
        payload = {
            "created_at": datetime.now(config.MARKET_TZ).isoformat(timespec="seconds"),
            "strategy": normalized_strategy,
            "ttl_minutes": int(self.ttl.total_seconds() // 60),
            "watchlist": watchlist,
            "regime": regime,
            "discovered_df": discovered_df,
        }
        with open(path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        self._memory_cache[path] = payload
        logger.info(f"✅ Discovery cache saved to {path} with {len(discovered_df)} ranked symbols.")
        return payload

    @staticmethod
    def _format_timedelta(value: timedelta) -> str:
        total_seconds = max(0, int(value.total_seconds()))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def get_cache_metadata(self, strategy: Optional[str] = None) -> Dict[str, Any]:
        now = datetime.now(config.MARKET_TZ)
        path = self._load_path_for_strategy(strategy)
        self.path = path
        metadata = {
            "path": path,
            "primary_path": self._cache_path_for_strategy(strategy),
            "legacy_fallback": path == DEFAULT_DISCOVERY_CACHE_FILE and self._normalize_strategy(strategy) in STRATEGY_DISCOVERY_CACHE_FILES,
            "exists": os.path.exists(path),
            "current_time": now,
            "generated_time": None,
            "age": None,
            "age_text": "N/A",
            "ttl": self.ttl,
            "ttl_text": self._format_timedelta(self.ttl),
            "remaining": None,
            "remaining_text": "N/A",
            "strategy": None,
            "expected_strategy": self._normalize_strategy(strategy),
            "stock_count": 0,
            "market_hours": self._is_market_hours(now),
            "valid": False,
            "invalid_reason": None,
            "load_error": None,
        }

        if not metadata["exists"]:
            metadata["invalid_reason"] = "missing file"
            return metadata

        payload = self.load_cache(strategy)
        if not payload:
            metadata["load_error"] = self._last_load_error
            metadata["invalid_reason"] = f"corrupted cache: {self._last_load_error}" if self._last_load_error else "empty cache payload"
            return metadata
        if not isinstance(payload, dict):
            metadata["invalid_reason"] = f"corrupted cache: expected dict payload, got {type(payload).__name__}"
            return metadata

        metadata["strategy"] = payload.get("strategy")
        try:
            created_at = datetime.fromisoformat(payload["created_at"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=config.MARKET_TZ)
        except Exception:
            metadata["invalid_reason"] = "corrupted cache: missing or invalid created_at timestamp"
            return metadata

        created_at = created_at.astimezone(config.MARKET_TZ)
        age = now - created_at
        remaining = self.ttl - age
        metadata["generated_time"] = created_at
        metadata["age"] = age
        metadata["age_text"] = self._format_timedelta(age)
        metadata["remaining"] = remaining
        metadata["remaining_text"] = self._format_timedelta(remaining)

        discovered_df = payload.get("discovered_df")
        if isinstance(discovered_df, pd.DataFrame):
            metadata["stock_count"] = len(discovered_df)

        if created_at.date() != now.date():
            metadata["invalid_reason"] = (
                f"wrong trading day: generated on {created_at.date()}, current market date is {now.date()}"
            )
            return metadata
        if age > self.ttl:
            metadata["invalid_reason"] = f"expired TTL: age {metadata['age_text']} exceeds TTL {metadata['ttl_text']}"
            return metadata
        if strategy and payload.get("strategy") != self._normalize_strategy(strategy):
            metadata["invalid_reason"] = (
                f"strategy mismatch: cache has {payload.get('strategy')}, requested {self._normalize_strategy(strategy)}"
            )
            return metadata
        if not isinstance(discovered_df, pd.DataFrame):
            metadata["invalid_reason"] = "corrupted cache: discovered_df is missing or not a DataFrame"
            return metadata
        if discovered_df.empty:
            metadata["invalid_reason"] = "empty discovery result: discovered_df has 0 rows"
            return metadata

        metadata["valid"] = True
        return metadata

    def log_cache_diagnostics(self, metadata: Dict[str, Any]) -> None:
        logger.warning(
            "Discovery cache invalid (%s). Path=%s | Generated=%s | Current=%s | Age=%s | TTL=%s | Strategy=%s | Expected=%s | Stocks=%s",
            metadata.get("invalid_reason"),
            metadata.get("path"),
            metadata["generated_time"].isoformat(timespec="seconds") if metadata.get("generated_time") else "N/A",
            metadata["current_time"].isoformat(timespec="seconds") if metadata.get("current_time") else "N/A",
            metadata.get("age_text", "N/A"),
            metadata.get("ttl_text", "N/A"),
            metadata.get("strategy") or "N/A",
            metadata.get("expected_strategy") or "N/A",
            metadata.get("stock_count", 0),
        )

    def is_cache_valid(self, strategy: Optional[str] = None) -> bool:
        metadata = self.get_cache_metadata(strategy)
        if not metadata["valid"]:
            self.log_cache_diagnostics(metadata)
        elif metadata.get("market_hours"):
            logger.info(
                "Discovery cache valid for %s during market hours. Path=%s | Generated=%s | Remaining=%s | Stocks=%s",
                metadata.get("expected_strategy") or metadata.get("strategy") or "ANY",
                metadata.get("path"),
                metadata["generated_time"].isoformat(timespec="seconds") if metadata.get("generated_time") else "N/A",
                metadata.get("remaining_text", "N/A"),
                metadata.get("stock_count", 0),
            )
        return metadata["valid"]

    def invalidate_cache(self, strategy: Optional[str] = None):
        paths = [self._cache_path_for_strategy(strategy)] if strategy else [
            *STRATEGY_DISCOVERY_CACHE_FILES.values(),
            DEFAULT_DISCOVERY_CACHE_FILE,
        ]
        for path in dict.fromkeys(paths):
            self._memory_cache.pop(path, None)
            if os.path.exists(path):
                os.remove(path)
        logger.info("Discovery cache invalidated.")

def get_cached_discovery_row(symbol: str, preferred_strategy: str = "SWING") -> Tuple[Optional[pd.Series], Optional[str]]:
    """Finds the full discovery data row for a symbol from any valid cache."""
    normalized_symbol = symbol.upper()
    # Start with the preferred strategy, then check others as a fallback.
    for strategy in dict.fromkeys([preferred_strategy, "BTST", "SWING", "GAP", "INTRADAY"]):
        cache = DiscoveryCache()
        if not cache.is_cache_valid(strategy):
            continue
        payload = cache.load_cache(strategy)
        discovered_df = payload.get("discovered_df") if payload else None
        if discovered_df is None or discovered_df.empty or "Symbol" not in discovered_df.columns:
            continue
        # Case-insensitive match on the symbol
        matched = discovered_df[discovered_df["Symbol"].astype(str).str.upper() == normalized_symbol]
        if not matched.empty:
            return matched.iloc[0], strategy  # Return the full row and which strategy cache it came from
    return None, None


def load_cached_discovery(strategy: Optional[str] = None, top_n: Optional[int] = None) -> Tuple[List[str], pd.DataFrame]:
    """Compatibility helper for Phase 2 callers that need cached discovery output."""
    cache = DiscoveryCache()
    if not cache.is_cache_valid(strategy):
        return [], pd.DataFrame()

    payload = cache.load_cache(strategy)
    if not payload:
        return [], pd.DataFrame()

    discovered_df = payload.get("discovered_df")
    if not isinstance(discovered_df, pd.DataFrame) or discovered_df.empty:
        return [], pd.DataFrame()

    watchlist = payload.get("watchlist")
    if not watchlist:
        watchlist = discovered_df["Symbol"].tolist() if "Symbol" in discovered_df.columns else []

    if top_n is not None:
        watchlist = watchlist[:top_n]
        if "Symbol" in discovered_df.columns:
            discovered_df = discovered_df[discovered_df["Symbol"].isin(watchlist)].copy()

    return watchlist, discovered_df

def save_last_signals(df_signals: pd.DataFrame, path: str = DEFAULT_LAST_SIGNALS_FILE) -> None:
    """Persists the latest displayed signals so `python main.py report` can run separately."""
    payload = {
        "created_at": datetime.now(config.MARKET_TZ).isoformat(timespec="seconds"),
        "signals": df_signals,
    }
    with open(path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)

def load_last_signals(path: str = DEFAULT_LAST_SIGNALS_FILE) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)

TOPN_HISTORY_FILE_TEMPLATE = "topn_history_{strategy}.pkl"


def _topn_history_path(strategy: str) -> str:
    return TOPN_HISTORY_FILE_TEMPLATE.format(strategy=strategy.lower())


def save_topn_history(top_n_df: pd.DataFrame, strategy: str, score_col: str) -> None:
    """Persists this run's top-N confirmed signals (Symbol/Score/Tier) for the
    next same-strategy run to diff against (see orchestrator.py's top-N
    drop-reason logging). Written atomically (temp file + os.replace, which
    is an atomic rename on both POSIX and Windows) so a crash mid-write can't
    corrupt the previous run's baseline - unlike save_last_signals above,
    this file is read back and relied on for correctness, not just convenience.
    """
    records = [
        {
            "Symbol": row["Symbol"],
            "Score": float(row.get(score_col, 0.0) or 0.0),
            "Tier": str(row.get("Strength", "UNKNOWN")),
        }
        for _, row in top_n_df.iterrows()
    ] if not top_n_df.empty else []

    payload = {
        "strategy": strategy,
        "saved_at": datetime.now(config.MARKET_TZ).isoformat(timespec="seconds"),
        "records": records,
    }
    path = _topn_history_path(strategy)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)


def load_topn_history(strategy: str) -> Optional[Dict[str, Any]]:
    path = _topn_history_path(strategy)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        logger.warning(f"Failed to load top-N history for {strategy}: {e}")
        return None

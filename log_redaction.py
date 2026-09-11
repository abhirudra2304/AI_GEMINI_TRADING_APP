"""Strips secrets out of log records before any handler writes them.

Why this exists: the vendored SmartAPI client interpolates the FULL request
headers into its error messages (SmartApi/smartConnect.py lines 221 and 246 use
`self.requestHeaders()` directly). Every AB1021 rate-limit error therefore wrote
the live `X-PrivateKey` and the complete `Authorization: Bearer <jwt>` to disk.
Measured 2026-09-11: the API key appeared in 90 log files and full bearer tokens
in 59. Those logs are gitignored and the key was verified absent from git
history, so the exposure was local-disk only - but under AB1021 contention the
client errors constantly, so the leak reproduced on essentially every scan.

Approach: patch `logging.Handler.handle` once, globally. Attaching filters to
individual loggers/handlers is the sanctioned mechanism but is not sufficient
here - logzero (which SmartAPI uses) creates its own logger with its own
handlers and `propagate = False`, and new handlers appear after this module is
imported. Patching the one method every record must pass through covers
handlers that do not exist yet, which is exactly the case that matters.

Redaction is applied to the record in place, so it also covers console output,
not just files.
"""
import logging
import os
import re
from typing import Iterable, Optional

MASK = "<redacted>"

# Header/JSON/kwarg shapes, each keeping the key visible and killing the value.
_PATTERNS = [
    # 'X-PrivateKey': 'abc123'  /  "X-PrivateKey": "abc123"
    re.compile(r"(['\"](?:X-PrivateKey|X-ClientPublicIP|X-MACAddress|Authorization|"
               r"jwtToken|refreshToken|feedToken|access_token|api_key|apikey|password|pin|totp)"
               r"['\"]\s*:\s*)(['\"])(?:(?!\2).)*(\2)", re.IGNORECASE),
    # Bearer <jwt>  (three dot-separated base64url segments, or any long blob)
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/=]{12,}", re.IGNORECASE),
    # bare JWTs that appear without the Bearer prefix
    re.compile(r"\beyJ[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}\b"),
    # KEY=value in env-style dumps
    re.compile(r"\b((?:GEMINI|ANGEL)_[A-Z_]*(?:KEY|PASSWORD|CODE|TOTP)\s*=\s*)\S+"),
]

# Exact live secret values, so anything echoing them in ANY shape is caught.
_SECRET_ENV_VARS = (
    "ANGEL_API_KEY", "ANGEL_PASSWORD", "ANGEL_TOTP_KEY",
    "ANGEL_CLIENT_CODE", "GEMINI_API_KEY",
)
_MIN_SECRET_LEN = 6   # never mask trivially short values - too many false hits


def _literal_secrets() -> list:
    out = []
    for name in _SECRET_ENV_VARS:
        val = os.getenv(name)
        if val and len(val) >= _MIN_SECRET_LEN:
            out.append(val)
    return sorted(set(out), key=len, reverse=True)


def redact(text: str, literals: Optional[Iterable[str]] = None) -> str:
    """Returns `text` with every known secret shape masked."""
    if not text:
        return text
    for lit in (literals if literals is not None else _literal_secrets()):
        if lit in text:
            text = text.replace(lit, MASK)
    for pat in _PATTERNS:
        if pat.groups >= 3:          # quoted key/value form: keep key + quotes
            text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}{m.group(3)}", text)
        elif pat.groups >= 1:
            text = pat.sub(lambda m: f"{m.group(1)}{MASK}", text)
        else:
            text = pat.sub(MASK, text)
    return text


def _redact_record(record: logging.LogRecord, literals) -> None:
    try:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg, literals)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: (redact(v, literals) if isinstance(v, str) else v)
                               for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(redact(a, literals) if isinstance(a, str) else a
                                    for a in record.args)
    except Exception:
        # Logging must never take the process down. A failed redaction is a bug
        # to fix, not a reason to lose the log line - but do not emit a record we
        # could not scrub, so blank it rather than risk leaking.
        record.msg = "<log record suppressed: redaction failed>"
        record.args = None


_installed = False


def install_redaction() -> bool:
    """Idempotent. Call once, as early as possible in process startup."""
    global _installed
    if _installed:
        return False
    original_handle = logging.Handler.handle

    def handle(self, record):
        _redact_record(record, _literal_secrets())
        return original_handle(self, record)

    logging.Handler.handle = handle
    _installed = True
    return True

"""
mpesa-bot/config.py

Single source of truth for all configuration.

Rules:
- Secrets (token, user ID) come from environment variables only.
  Never hardcode them. _require() fails immediately at startup if
  a required var is missing — better to crash on boot than to run
  silently broken.

- Non-secret settings have sensible defaults so the app runs
  locally without a full env setup.

- trust_level_for() lives here because trust level is fully
  determined by source. It was intentionally removed from the DB
  schema to avoid two copies that could drift. App code that needs
  trust level calls this function.

Environment variables:
  MPESA_BOT_TOKEN      Telegram bot token (required)
  MPESA_BOT_USER_ID    Telegram chat ID of the single authorised user (required)
  MPESA_DB_PATH        Path to SQLite database file (default: mpesa.db)
  MPESA_MAX_ATTEMPTS   Max processing retries before abandoning (default: 3)
"""

import os


# ------------------------------------------------------------------ #
# Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _require(key: str) -> str:
    """
    Read a required environment variable.
    Raises EnvironmentError immediately if it is missing or empty.
    This surfaces misconfiguration at startup, not mid-request.
    """
    val = os.environ.get(key, "").strip()
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Add it to your .env file or shell environment."
        )
    return val


def _int_env(key: str, default: int) -> int:
    """Read an optional integer env var with a fallback default."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise EnvironmentError(
            f"Environment variable '{key}' must be an integer, got: {raw!r}"
        )


# ------------------------------------------------------------------ #
# Secrets (required)                                                  #
# ------------------------------------------------------------------ #

BOT_TOKEN: str = _require("MPESA_BOT_TOKEN")
BOT_USER_ID: str = _require("MPESA_BOT_USER_ID")


# ------------------------------------------------------------------ #
# Non-secret settings (optional with defaults)                        #
# ------------------------------------------------------------------ #

DB_PATH: str = os.environ.get("MPESA_DB_PATH", "mpesa.db").strip() or "mpesa.db"

# How many times the pipeline retries a failing raw event before
# giving up and leaving it visible in /unknown.
MAX_PROCESS_ATTEMPTS: int = _int_env("MPESA_MAX_ATTEMPTS", 3)

# Ceiling on raw_text length. M-Pesa SMS are ~160 chars.
# 1000 is generous enough for any real message; anything above
# this is not a legitimate SMS and should be rejected at ingestion.
MAX_RAW_TEXT_LENGTH: int = 1000


# ------------------------------------------------------------------ #
# Trust level derivation                                              #
# ------------------------------------------------------------------ #

# Valid sources and their trust levels.
# This mapping is the single authoritative definition.
# The DB does not store trust_level — it is always derived here.
_SOURCE_TRUST: dict[str, str] = {
    "manual": "user_verified",
    "auto":   "auto_unverified",
}

VALID_SOURCES: frozenset[str] = frozenset(_SOURCE_TRUST.keys())


def trust_level_for(source: str) -> str:
    """
    Return the trust level for a given source.

    Raises ValueError if source is not a known value.
    Call this rather than hardcoding the mapping elsewhere.

    >>> trust_level_for("manual")
    'user_verified'
    >>> trust_level_for("auto")
    'auto_unverified'
    """
    try:
        return _SOURCE_TRUST[source]
    except KeyError:
        raise ValueError(
            f"Unknown source {source!r}. Valid sources: {sorted(VALID_SOURCES)}"
        )

"""Paths, network settings and tunables shared across modules."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("SCREENER_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
EDGAR_CACHE = CACHE_DIR / "edgar"
PRICE_CACHE = CACHE_DIR / "prices"
UNIVERSE_FILE = DATA_DIR / "universe.csv"

# SEC requires a descriptive User-Agent with a contact address, or it returns 403.
# https://www.sec.gov/os/accessing-edgar-data
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "")
SEC_MAX_RPS = 8  # SEC hard limit is 10/s; stay under it.

EDGAR_MAX_AGE_DAYS = 7  # filings change quarterly; weekly refresh is plenty
PRICE_MAX_AGE_HOURS = 12

# Exchanges we accept. SEC's ticker file labels these "NYSE" and "Nasdaq";
# OTC, CBOE and unlisted names are dropped.
ALLOWED_EXCHANGES = {"NYSE", "Nasdaq"}


def require_sec_agent() -> str:
    if not SEC_USER_AGENT or "@" not in SEC_USER_AGENT:
        raise SystemExit(
            "Set SEC_USER_AGENT to 'Name email@example.com' (SEC rejects anonymous requests)."
        )
    return SEC_USER_AGENT

"""Paths, network settings and tunables shared across modules."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("SCREENER_HOME", Path(__file__).resolve().parent.parent))
# Working caches (~2GB) live on local disk, not the shared network folder; db/ is the durable copy (store.py).
DATA_DIR = Path(os.environ.get("SCREENER_DATA", Path.home() / "screener-data"))
CACHE_DIR = DATA_DIR / "cache"
EDGAR_CACHE = CACHE_DIR / "edgar"
PRICE_CACHE = CACHE_DIR / "prices"
UNIVERSE_FILE = DATA_DIR / "universe.csv"
UNIVERSE_WIDE_FILE = DATA_DIR / "universe_wide.csv"
# "index" (default): S&P 500/400/600 + Nasdaq-100 members. "wide": every listed US company in the
# size band (screener/wide.py). Wide stays opt-in until it beats index in the backtest.
UNIVERSE = os.environ.get("QUAINT_UNIVERSE", "index")

# SEC requires a descriptive User-Agent with a contact address, or it returns 403.
# https://www.sec.gov/os/accessing-edgar-data
# Set via env var, or put it in ./sec_user_agent.txt (gitignored).
_UA_FILE = ROOT / "sec_user_agent.txt"
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT") or (
    _UA_FILE.read_text().strip() if _UA_FILE.exists() else "")
SEC_MAX_RPS = 8  # SEC hard limit is 10/s; stay under it.

EDGAR_MAX_AGE_DAYS = 7  # filings change quarterly; weekly refresh is plenty
PRICE_MAX_AGE_HOURS = 12

# Exchanges we accept. SEC's ticker file labels these "NYSE" and "Nasdaq";
# Cboe-listed index members (e.g. CBOE itself) are liquid; OTC and unlisted names are dropped.
ALLOWED_EXCHANGES = {"NYSE", "Nasdaq", "CBOE"}


def require_sec_agent() -> str:
    if not SEC_USER_AGENT or "@" not in SEC_USER_AGENT:
        raise SystemExit(
            "Set SEC_USER_AGENT to 'Name email@example.com' (SEC rejects anonymous requests)."
        )
    return SEC_USER_AGENT

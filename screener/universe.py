"""Build the investable universe: S&P 500 + S&P 400 + Nasdaq-100, mapped to SEC CIKs.

Index membership and GICS sectors come from Wikipedia (free, maintained, good enough
for a screener; not point-in-time, so the backtest has survivorship bias we must
account for). CIK and listing exchange come from SEC's own ticker file.
"""
from __future__ import annotations

import io

import pandas as pd

from . import config, http

WIKI = {
    "SP500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "SP400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "NDX": "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
}
SEC_TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"

_TICKER_COLS = ("Symbol", "Ticker", "Ticker symbol")
_SECTOR_COLS = ("GICS Sector", "GICS sector", "ICB Industry[1]", "ICB Industry")
_INDUSTRY_COLS = ("GICS Sub-Industry", "GICS Sub-industry", "GICS sub-industry",
                  "ICB Subsector[1]", "ICB Subsector")

# The Nasdaq-100 page uses ICB. Map to GICS sectors so sector-relative scoring has one
# taxonomy. Only Nasdaq-100 names outside the S&P 500/400 end up with these labels.
_ICB_TO_GICS = {
    "Technology": "Information Technology", "Telecommunications": "Communication Services",
    "Basic Materials": "Materials",
}


def norm_ticker(t: str) -> str:
    """BRK.B / BRK B -> BRK-B (the form both SEC and Yahoo use)."""
    return str(t).strip().upper().replace(".", "-").replace(" ", "-")


def _pick(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _index_members(name: str, url: str) -> pd.DataFrame:
    html = http.get(url, headers={"User-Agent": "Mozilla/5.0 stock-screener"}).text
    for t in pd.read_html(io.StringIO(html)):
        tick = _pick(t, _TICKER_COLS)
        sector = _pick(t, _SECTOR_COLS)
        if tick and sector and len(t) > 50:
            ind = _pick(t, _INDUSTRY_COLS)
            out = pd.DataFrame({
                "ticker": t[tick].map(norm_ticker),
                "sector": t[sector].astype(str).replace(_ICB_TO_GICS),
                "industry": t[ind].astype(str) if ind else "",
            })
            out["index"] = name
            return out
    raise RuntimeError(f"no constituents table found at {url}; page layout changed")


def sec_ticker_map() -> pd.DataFrame:
    js = http.get(SEC_TICKERS, headers={"User-Agent": config.require_sec_agent()},
                  max_rps=config.SEC_MAX_RPS).json()
    df = pd.DataFrame(js["data"], columns=js["fields"])
    df["ticker"] = df["ticker"].map(norm_ticker)
    # A CIK can list several tickers (share classes); keep each ticker row.
    return df.rename(columns={"name": "sec_name"})[["ticker", "cik", "sec_name", "exchange"]]


def build(save: bool = True) -> pd.DataFrame:
    parts = [_index_members(n, u) for n, u in WIKI.items()]
    members = pd.concat(parts, ignore_index=True)
    # One row per ticker; remember every index it belongs to.
    idx = members.groupby("ticker")["index"].agg(lambda s: ",".join(sorted(set(s))))
    first = members.drop_duplicates("ticker").set_index("ticker")[["sector", "industry"]]
    uni = first.join(idx).reset_index()

    sec = sec_ticker_map()
    uni = uni.merge(sec, on="ticker", how="left")
    missing = uni[uni["cik"].isna()]["ticker"].tolist()
    if missing:
        print(f"[universe] {len(missing)} tickers not in SEC map, dropped: {missing[:15]}")
    uni = uni.dropna(subset=["cik"])
    uni["cik"] = uni["cik"].astype(int)
    off = uni[~uni["exchange"].isin(config.ALLOWED_EXCHANGES)]
    if len(off):
        print(f"[universe] dropping {len(off)} not on NYSE/Nasdaq: {off['ticker'].tolist()[:15]}")
    uni = uni[uni["exchange"].isin(config.ALLOWED_EXCHANGES)].reset_index(drop=True)

    if save:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        uni.to_csv(config.UNIVERSE_FILE, index=False)
    return uni


# GICS misfits where the official sector would score a company against the wrong peers.
# FSLR is GICS "Semiconductors" (it makes thin-film cells) but sells utility-scale solar
# modules; vs. NVDA/AVGO multiples it always looks cheap. Its economic peers (NXT, GEV)
# sit in Industrials / Electrical Components & Equipment.
SECTOR_OVERRIDES = {
    "FSLR": ("Industrials", "Electrical Components & Equipment"),
}


def load() -> pd.DataFrame:
    uni = build() if not config.UNIVERSE_FILE.exists() else pd.read_csv(config.UNIVERSE_FILE)
    for t, (sector, industry) in SECTOR_OVERRIDES.items():
        uni.loc[uni["ticker"] == t, ["sector", "industry"]] = [sector, industry]
    return uni

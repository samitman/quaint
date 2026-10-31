"""The wide universe: every US-headquartered company listed on NYSE/Nasdaq/Cboe in a broad size
band, not only S&P 500/400/600 and Nasdaq-100 members.

Index members keep their GICS sectors from Wikipedia. The rest get a sector mapped from their
SEC SIC code (coarser, but the same eleven sector names), so sector-relative scoring and the
sector cap still work. A cheap prefilter (SEC shares outstanding x a month of Yahoo prices)
keeps the list to companies the size, price and liquidity gates could ever pass, with room
for the backtest: a company now worth $8B may have been $3B in 2019.
"""
from __future__ import annotations

import json
import re

import pandas as pd

from . import config, http

FRAMES = "https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{}.json"
MIN_MCAP, MAX_MCAP = 100e6, 10e9  # wider than the screen's $0.3-5B gate on purpose
MIN_PRICE, MIN_ADV = 3.0, 1e6

# Warrants, rights, units and preferreds share a CIK with the common stock.
_NOT_COMMON = re.compile(r"-(?:W|WS|WT|U|UN|R|RT|P.*)$|^[A-Z]{4}[WUR]$")

# SIC code ranges -> GICS sector, first match wins. Coarse by design.
_SIC = [
    ((1311, 1389), "Energy"), ((1220, 1241), "Energy"), ((2900, 2999), "Energy"), ((5171, 5172), "Energy"),
    ((1000, 1499), "Materials"),
    ((1531, 1531), "Consumer Discretionary"),  # operative builders = homebuilders
    ((1500, 1799), "Industrials"),
    ((100, 999), "Consumer Staples"), ((2000, 2199), "Consumer Staples"), ((2840, 2844), "Consumer Staples"),
    ((5140, 5149), "Consumer Staples"), ((5400, 5499), "Consumer Staples"), ((5912, 5912), "Consumer Staples"),
    ((2830, 2836), "Health Care"), ((3841, 3851), "Health Care"), ((5047, 5047), "Health Care"),
    ((5122, 5122), "Health Care"), ((8000, 8099), "Health Care"), ((8731, 8731), "Health Care"),
    ((2200, 2399), "Consumer Discretionary"), ((2500, 2599), "Consumer Discretionary"),
    ((3010, 3011), "Consumer Discretionary"), ((3100, 3199), "Consumer Discretionary"),
    ((3630, 3639), "Consumer Discretionary"), ((3651, 3652), "Consumer Discretionary"),
    ((3711, 3716), "Consumer Discretionary"), ((3750, 3759), "Consumer Discretionary"),
    ((3790, 3799), "Consumer Discretionary"), ((3873, 3873), "Consumer Discretionary"),
    ((3900, 3999), "Consumer Discretionary"), ((5200, 5999), "Consumer Discretionary"),
    ((7000, 7099), "Consumer Discretionary"), ((7200, 7299), "Consumer Discretionary"),
    ((7500, 7599), "Consumer Discretionary"), ((7900, 7999), "Consumer Discretionary"),
    ((8200, 8299), "Consumer Discretionary"),
    ((2700, 2799), "Communication Services"), ((4800, 4899), "Communication Services"),
    ((7310, 7319), "Communication Services"), ((7800, 7899), "Communication Services"),
    ((3570, 3579), "Information Technology"), ((3660, 3679), "Information Technology"),
    ((3820, 3829), "Information Technology"), ((3861, 3861), "Information Technology"),
    ((7370, 7379), "Information Technology"),
    ((2400, 2499), "Materials"), ((2600, 2699), "Materials"), ((2800, 2899), "Materials"),
    ((3000, 3099), "Materials"), ((3200, 3399), "Materials"),
    ((4953, 4955), "Industrials"), ((4900, 4999), "Utilities"),
    ((6500, 6553), "Real Estate"), ((6798, 6798), "Real Estate"),
    ((6000, 6799), "Financials"),
    ((3400, 3999), "Industrials"), ((4000, 4799), "Industrials"), ((5000, 5199), "Industrials"),
    ((7300, 7399), "Industrials"), ((8700, 8799), "Industrials"),
]
_SKIP_SIC = {6770}  # blank checks (SPACs)


def sic_sector(sic) -> str | None:
    try:
        s = int(sic)
    except (TypeError, ValueError):
        return None
    if s in _SKIP_SIC:
        return None
    return next((sec for (lo, hi), sec in _SIC if lo <= s <= hi), None)


def submission_meta(cik: int) -> dict:
    """SIC code, its description and business address from SEC submissions. Cached."""
    p = config.CACHE_DIR / "sic" / f"{int(cik)}.json"
    if p.exists():
        return json.loads(p.read_text())
    js = http.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                  headers={"User-Agent": config.require_sec_agent()}, max_rps=config.SEC_MAX_RPS).json()
    addr = js.get("addresses", {}).get("business", {}) or {}
    meta = {"sic": js.get("sic") or "", "sic_desc": js.get("sicDescription") or "",
            "hq": "FOREIGN" if addr.get("isForeignLocation") == 1 else (addr.get("stateOrCountry") or "")}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta))
    return meta


def _shares() -> pd.DataFrame:
    """Latest dei shares outstanding per CIK, with SEC's location code, from two quarterly frames."""
    now = pd.Timestamp.now()
    out = []
    for back in range(1, 4):  # the last completed quarters; the newest may still be filling in
        q = (now - pd.DateOffset(months=3 * back)).to_period("Q")
        try:
            js = http.get(FRAMES.format(f"CY{q.year}Q{q.quarter}I"),
                          headers={"User-Agent": config.require_sec_agent()}, max_rps=config.SEC_MAX_RPS).json()
        except Exception as e:
            print(f"[wide] frame CY{q.year}Q{q.quarter}I unavailable: {e}")
            continue
        out.append(pd.DataFrame(js["data"])[["cik", "loc", "end", "val"]])
    df = pd.concat(out).sort_values("end").drop_duplicates("cik", keep="last")
    return df.rename(columns={"val": "shares_dei"}).set_index("cik")


def extras(sec: pd.DataFrame, known_ciks: set[int]) -> pd.DataFrame:
    """Listed US companies outside the indexes that could ever pass the size/liquidity gates."""
    import yfinance as yf

    sec = sec[sec["exchange"].isin(config.ALLOWED_EXCHANGES) & ~sec["cik"].isin(known_ciks)]
    sec = sec[~sec["ticker"].str.contains(_NOT_COMMON)]
    sec = sec.drop_duplicates("cik")  # SEC lists a company's primary class first
    sh = _shares()
    df = sec.join(sh, on="cik", how="inner")
    df = df[df["loc"].fillna("").str.match(r"^US-") & ~df["loc"].isin({"US-PR", "US-VI", "US-GU"})]
    print(f"[wide] {len(df)} listed US companies outside the indexes; pricing them")
    rows = []
    tickers = df["ticker"].tolist()
    for i in range(0, len(tickers), 200):
        batch = tickers[i:i + 200]
        px = yf.download(batch, period="1mo", auto_adjust=False, group_by="column", threads=True, progress=False)
        if px.empty:
            continue
        close, vol = px["Close"], px["Volume"]
        if isinstance(close, pd.Series):
            close, vol = close.to_frame(batch[0]), vol.to_frame(batch[0])
        last = close.ffill().iloc[-1]
        adv = (close * vol).median()
        rows.append(pd.DataFrame({"last": last, "adv": adv}))
    q = pd.concat(rows) if rows else pd.DataFrame(columns=["last", "adv"])
    df = df.join(q, on="ticker")
    df["mcap_est"] = df["last"] * df["shares_dei"]
    keep = df["mcap_est"].between(MIN_MCAP, MAX_MCAP) & (df["last"] > MIN_PRICE) & (df["adv"] > MIN_ADV)
    df = df[keep].copy()
    print(f"[wide] {len(df)} pass the size/price/liquidity prefilter; reading SIC codes")
    meta = pd.DataFrame([submission_meta(c) for c in df["cik"]], index=df.index)
    df = df.join(meta)
    df["sector"] = df["sic"].map(sic_sector)
    df["industry"] = df["sic_desc"]
    # SIC 6798 lumps mortgage REITs in with property REITs; the name is the best cheap tell.
    mreit = (df["sic"].astype(str) == "6798") & df["sec_name"].str.contains(r"mortgage|mtg|capital trust|finance", case=False)
    df.loc[mreit, "industry"] = "Mortgage REITs"
    dropped = df["sector"].isna().sum()
    if dropped:
        print(f"[wide] {dropped} without a usable SIC sector (SPACs, shells), dropped")
    df = df.dropna(subset=["sector"])
    df["index"] = ""
    df["us_hq"] = df["hq"].str.match(r"^[A-Z]{2}$") & ~df["hq"].isin({"PR", "VI", "GU"})
    print(f"[wide] added {len(df)} companies")
    return df[["ticker", "sector", "industry", "index", "cik", "sec_name", "exchange", "hq", "us_hq"]].reset_index(drop=True)

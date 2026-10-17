"""Build the scored cross-section for one date: live (as_of=None) or historical."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from . import config, edgar, fundamentals, metrics, prices, score, universe

_STATE: dict = {}
LATEST = config.DATA_DIR / "latest_xs.pkl"


def _init(uni, px):
    _STATE["uni"], _STATE["px"] = uni, px
    _STATE["cf"] = {}


def _company_rows(args):
    """All requested dates for one company (parsed facts are reused across dates)."""
    cik, dates = args
    uni, px = _STATE["uni"], _STATE["px"]
    cf = edgar.fetch(cik)
    if not cf:
        return []
    rows = []
    for t, r in uni[uni["cik"] == cik].iterrows():
        if t not in px["Close"].columns:
            continue
        close, splits = px["Close"][t], px["Stock Splits"][t].fillna(0)
        for d in dates:
            c = close[:d].dropna()
            if c.empty or (d - c.index[-1]).days > 10:
                continue  # not trading at d
            snap = fundamentals.snapshot(cf, as_of=d).values
            m = metrics.compute(snap, t, r["sector"], float(c.iloc[-1]), as_of=d, splits=splits)
            m["date"], m["industry"] = d, r["industry"]
            rows.append(m)
    return rows


def cross_sections(dates: list[pd.Timestamp], uni: pd.DataFrame | None = None,
                   px: dict | None = None, workers: int = 4) -> pd.DataFrame:
    """Unscored metrics for every (date, ticker). Point-in-time on fundamentals."""
    uni = universe.load() if uni is None else uni
    uni = uni.set_index("ticker") if "ticker" in uni.columns else uni
    px = prices.load(uni.index.tolist()) if px is None else px
    jobs = [(int(c), dates) for c in uni["cik"].unique()]
    rows = []
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(uni, px)) as ex:
        for i, part in enumerate(ex.map(_company_rows, jobs, chunksize=8), 1):
            rows.extend(part)
    df = pd.DataFrame(rows)
    # Liquidity: median dollar volume over the prior ~3 months, in traded dollars.
    dollar = px["Close"] * px["Volume"]
    adv = dollar.rolling(63, min_periods=40).median()
    df["adv_usd"] = [adv[t].asof(d) if t in adv else None for t, d in zip(df["ticker"], df["date"])]
    return df


def _fill_missing_shares(df: pd.DataFrame) -> None:
    """A few multi-class issuers (STZ, BRK) tag no usable undimensioned share count.
    Live only, fall back to Yahoo's count; the backtest just skips them."""
    miss = df["shares"].isna() & df["price"].notna()
    if not miss.any():
        return
    import yfinance as yf
    for i in df.index[miss]:
        try:
            # marketCap counts every class (BRK A+B, STZ A+B); fast_info shares don't.
            sh = yf.Ticker(df.at[i, "ticker"]).info["marketCap"] / df.at[i, "price"]
        except Exception:
            continue
        if sh:
            df.at[i, "shares"], df.at[i, "shares_source"] = float(sh), "yahoo"
            df.at[i, "mcap"] = df.at[i, "price"] * sh
            for c, num in (("earnings_yield", "net_income"), ("fcf_yield", "fcf"), ("book_yield", "equity")):
                if pd.notna(df.at[i, num]):
                    df.at[i, c] = df.at[i, num] / df.at[i, "mcap"]
            ev = df.at[i, "mcap"] + df.at[i, "net_debt"]
            df.at[i, "ev"] = ev
            e = df.at[i, "ebit"]
            df.at[i, "ebit_ev"] = e / ev if pd.notna(e) and ev > 0 else None
            df.at[i, "ev_ebit"] = ev / e if pd.notna(e) and e > 0 and ev > 0 else None
            n, q = df.at[i, "net_income"], df.at[i, "equity"]
            df.at[i, "pe"] = df.at[i, "mcap"] / n if pd.notna(n) and n > 0 else None
            df.at[i, "pb"] = df.at[i, "mcap"] / q if pd.notna(q) and q > 0 else None


def run(as_of: pd.Timestamp | None = None, weights: dict | None = None) -> pd.DataFrame:
    """Live screen: one scored cross-section at the latest price date."""
    uni = universe.load().set_index("ticker")
    px = prices.load(uni.index.tolist())
    d = pd.Timestamp(as_of) if as_of is not None else px["Close"].index[-1]
    df = cross_sections([d], uni, px)
    _fill_missing_shares(df)
    df = df.join(uni[["sec_name", "index", "exchange"]], on="ticker")
    df.to_pickle(LATEST)  # unscored, so `lookup` can add a ticker and re-score
    return score.score(df.set_index("ticker"), weights).sort_values("composite", ascending=False)

"""Build the scored cross-section for one date: live (as_of=None) or historical."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from . import config, edgar, fundamentals, metrics, prices, score, universe, yahoo_fill

_STATE: dict = {}
LATEST = config.DATA_DIR / "latest_xs.pkl"


def _init(uni, px, live=False):
    _STATE["uni"], _STATE["px"], _STATE["live"] = uni, px, live
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
            m = metrics.compute(snap, t, r["sector"], float(c.iloc[-1]), as_of=d, splits=splits,
                                    industry=r["industry"])
            if _STATE.get("live"):
                used = yahoo_fill.patch(snap, t, d, m["debt_estimated"])
                if used:
                    m = metrics.compute(snap, t, r["sector"], float(c.iloc[-1]), as_of=d, splits=splits,
                                    industry=r["industry"])
                m["yahoo_fields"] = ",".join(used)
            m["date"], m["industry"] = d, r["industry"]
            rows.append(m)
    return rows


def cross_sections(dates: list[pd.Timestamp], uni: pd.DataFrame | None = None,
                   px: dict | None = None, workers: int = 4, live: bool = False) -> pd.DataFrame:
    """Unscored metrics for every (date, ticker). Point-in-time on fundamentals."""
    uni = universe.load() if uni is None else uni
    uni = uni.set_index("ticker") if "ticker" in uni.columns else uni
    px = prices.load(uni.index.tolist()) if px is None else px
    jobs = [(int(c), dates) for c in uni["cik"].unique()]
    rows = []
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(uni, px, live)) as ex:
        for i, part in enumerate(ex.map(_company_rows, jobs, chunksize=8), 1):
            rows.extend(part)
            if len(dates) > 1 and i % 100 == 0:
                print(f"[history] {i}/{len(jobs)} companies", flush=True)
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
            for c, num in (("earnings_yield", "net_income"), ("earnings_yield_norm", "net_income_norm"), ("fcf_yield", "fcf"), ("book_yield", "equity"),
                           ("fcf_acq_yield", "fcf_post_acq"), ("shareholder_yield", "payout")):
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
            r = df.at[i, "revenue"]
            df.at[i, "ps"] = df.at[i, "mcap"] / r if pd.notna(r) and r > 0 else None


def run(as_of: pd.Timestamp | None = None, weights: dict | None = None) -> pd.DataFrame:
    """Live screen: one scored cross-section at the latest price date."""
    uni = universe.load().set_index("ticker")
    px = prices.load(uni.index.tolist())
    if as_of is None:
        px = prices.update_today(px)
    d = pd.Timestamp(as_of) if as_of is not None else px["Close"].index[-1]
    df = cross_sections([d], uni, px, live=True)
    _fill_missing_shares(df)
    df = df.join(uni[["sec_name", "index", "exchange", "us_hq"]], on="ticker")
    df = df.join(prices.performance(px, d), on="ticker").set_index("ticker")
    inv = score.score(df, weights).query("investable").index  # only these get per-company SEC calls
    df = enrich(df, uni["cik"], d, list(inv))
    df.reset_index().to_pickle(LATEST)  # unscored, so `lookup` can add a ticker and re-score
    return score.score(df, weights).sort_values("composite", ascending=False)


def enrich(df: pd.DataFrame, ciks: pd.Series, d, tickers: list) -> pd.DataFrame:
    """Insider buying/selling (Form 4) and 8-K events. df is indexed by ticker."""
    from . import events, insider
    trades = insider.update()
    trades = insider.recent(trades, {t: ciks[t] for t in tickers if t in ciks and pd.notna(ciks[t])})
    df = insider.add(df, trades, ciks, d)
    return events.add(df, ciks, d, tickers=[t for t in tickers if t in df.index])

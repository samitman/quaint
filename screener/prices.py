"""Daily prices from Yahoo via yfinance, cached to CSV.

Only prices come from Yahoo. Its fundamentals are scraped, unversioned and shallow;
EDGAR is the source of truth for anything from a filing.
"""
from __future__ import annotations

import time

import pandas as pd

from . import config

_FIELDS = ("Close", "Adj Close", "Volume", "Stock Splits")


def _cache_path(field: str, name: str = ""):
    return config.PRICE_CACHE / f"{name}{field.replace(' ', '_').lower()}.csv"


def _fresh(name: str = "") -> bool:
    p = _cache_path("Close", name)
    return p.exists() and (time.time() - p.stat().st_mtime) < config.PRICE_MAX_AGE_HOURS * 3600


def load(tickers: list[str], *, period: str = "10y", refresh: bool = False,
         chunk: int = 100, name: str = "") -> dict[str, pd.DataFrame]:
    """Return {'Close', 'Adj Close', 'Volume'} -> DataFrame (dates x tickers).

    Yahoo's Close is split-adjusted to today's share basis (not dividend-adjusted), so a
    historical market cap needs share counts restated with `split_factor_after`.
    Adj Close adds dividends (use it for returns). Stock Splits is the ratio on ex-date, 0 otherwise.
    `name` prefixes the cache files, so a benchmark download doesn't replace the universe's.
    """
    tickers = sorted(set(tickers))
    from . import db
    if db.enabled():  # the warehouse is the source of truth; downloads land there too
        from . import warehouse
        key = f"prices_refreshed:{name or 'universe'}"
        if not refresh and db.get_meta(key, config.PRICE_MAX_AGE_HOURS):
            out = warehouse.load_prices(tickers)
            missing = sum(not out["Close"][t].notna().any() for t in tickers)
            if missing <= 0.02 * len(tickers):  # a few index names never trade on Yahoo
                return out
    if not refresh and _fresh(name):
        out = {f: pd.read_csv(_cache_path(f, name), index_col=0, parse_dates=True) for f in _FIELDS}
        if set(tickers) <= set(out["Close"].columns):
            return {f: df[tickers] for f, df in out.items()}

    import yfinance as yf  # imported lazily so offline modules/tests don't need it

    frames = {f: [] for f in _FIELDS}
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        df = yf.download(batch, period=period, auto_adjust=False, actions=True,
                         group_by="column", threads=True, progress=False)
        for f in _FIELDS:
            if f not in df.columns.get_level_values(0):
                part = pd.DataFrame(0.0, index=df.index, columns=batch)
            else:
                part = df[f]
            if isinstance(part, pd.Series):  # single-ticker batch
                part = part.to_frame(batch[0])
            frames[f].append(part)
        print(f"[prices] {min(i + chunk, len(tickers))}/{len(tickers)}")

    out = {f: pd.concat(v, axis=1).sort_index() for f, v in frames.items()}
    empty = [t for t in tickers if out["Close"][t].dropna().empty] if tickers else []
    if empty:
        print(f"[prices] no data for {len(empty)}: {empty[:15]}")
    config.PRICE_CACHE.mkdir(parents=True, exist_ok=True)
    for f, df in out.items():
        df.to_csv(_cache_path(f, name))
    if db.enabled():
        warehouse.sync_prices(out)
        db.set_meta(key, pd.Timestamp.now(tz="UTC").isoformat())
    return out


def update_today(px: dict[str, pd.DataFrame], chunk: int = 200) -> dict[str, pd.DataFrame]:
    """Overwrite/append the latest session with live intraday data.

    Yahoo's daily history leaves the current session as NaN until it's finalised (often
    the next morning), so a daily download silently gives yesterday's close. One-minute
    bars for today are available immediately, in batches.
    """
    import yfinance as yf

    tickers = list(px["Close"].columns)
    closes, vols = [], []
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        df = yf.download(batch, period="1d", interval="1m", auto_adjust=False, prepost=False,
                         group_by="column", threads=True, progress=False)
        if df.empty:
            continue
        c, v = df["Close"], df["Volume"]
        if isinstance(c, pd.Series):
            c, v = c.to_frame(batch[0]), v.to_frame(batch[0])
        closes.append(c.ffill().iloc[-1])
        vols.append(v.sum())
        day = pd.Timestamp(c.index[-1].date())
    if not closes:
        return px
    last, vol = pd.concat(closes), pd.concat(vols)
    out = {}
    for f, df in px.items():
        df = df.copy()
        if day not in df.index:
            df.loc[day] = float("nan")
            df = df.sort_index()
        out[f] = df
    prev_close = out["Close"][out["Close"].index < day].ffill().iloc[-1]
    prev_adj = out["Adj Close"][out["Adj Close"].index < day].ffill().iloc[-1]
    ok = last.dropna().index.intersection(out["Close"].columns)
    out["Close"].loc[day, ok] = last[ok]
    # Adjustment factor carries over from the prior session (exact unless ex-div today).
    out["Adj Close"].loc[day, ok] = (prev_adj[ok] / prev_close[ok] * last[ok]).values
    out["Volume"].loc[day, ok] = vol.reindex(ok).values
    out["Stock Splits"].loc[day] = out["Stock Splits"].loc[day].fillna(0)
    print(f"[prices] live prices for {len(ok)} tickers as of {c.index[-1]}")
    return out


def split_factor_after(splits: pd.Series, when) -> float:
    """Product of split ratios with ex-date after `when`: multiply a share count reported
    at `when` by this to put it on the same basis as Yahoo's split-adjusted Close."""
    s = splits[(splits.index > pd.Timestamp(when)) & (splits > 0)]
    return float(s.prod()) if len(s) else 1.0


def liquidity(px: dict[str, pd.DataFrame], window: int = 63) -> pd.DataFrame:
    """Last close and median daily dollar volume over ~3 months, per ticker."""
    close, vol = px["Close"], px["Volume"]
    dollar = (close * vol).tail(window)
    return pd.DataFrame({
        "price": close.ffill().iloc[-1],
        "adv_usd": dollar.median(),
        "last_trade": close.apply(lambda s: s.last_valid_index()),
    })


def performance(px: dict[str, pd.DataFrame], d: pd.Timestamp) -> pd.DataFrame:
    """Total return (dividends included) over 1 month, YTD and 1 year to `d`, plus
    distance from the 52-week high, per ticker."""
    adj, close = px["Adj Close"][:d], px["Close"][:d]

    def at(when):
        return adj[:when].ffill().iloc[-1] if len(adj[:when]) else pd.Series(index=adj.columns, dtype=float)

    now = adj.ffill().iloc[-1]
    year = close[close.index > d - pd.DateOffset(years=1)]
    return pd.DataFrame({
        "ret_1m": now / at(d - pd.DateOffset(months=1)) - 1,
        "ret_ytd": now / at(pd.Timestamp(d.year - 1, 12, 31)) - 1,
        "ret_1y": now / at(d - pd.DateOffset(years=1)) - 1,
        "off_high": close.ffill().iloc[-1] / year.max() - 1,
        # 12-1 momentum: last year's return skipping the latest month (short-term reversal).
        "mom_12_1": at(d - pd.DateOffset(months=1)) / at(d - pd.DateOffset(years=1)) - 1,
    })

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

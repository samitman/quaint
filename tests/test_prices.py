"""Yahoo drops failed tickers from the split columns but not from Close; every field must still
cover every ticker, and failed tickers get one retry."""
import sys
import types

import numpy as np
import pandas as pd

from screener import config, db, prices

DAYS = pd.date_range("2026-10-01", periods=3)


def fake_yf(fail_first: set):
    calls = []

    def download(batch, **kw):
        calls.append(list(batch))
        bad = fail_first if len(calls) == 1 else set()
        ok = [t for t in batch if t not in bad]
        cols = {}
        for f in ("Adj Close", "Close", "Volume"):
            for t in batch:
                cols[(f, t)] = [np.nan] * 3 if t in bad else [10.0, 11.0, 12.0]
        for f in ("Dividends", "Stock Splits"):  # actions only for tickers that returned data
            for t in ok:
                cols[(f, t)] = [0.0] * 3
        return pd.DataFrame(cols, index=DAYS)

    return types.SimpleNamespace(download=download), calls


def test_failed_tickers_retried_and_fields_squared(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PRICE_CACHE", tmp_path)
    monkeypatch.setattr(prices.time, "sleep", lambda s: None)
    monkeypatch.setattr(db, "URL", "")
    yf, calls = fake_yf({"BBB"})
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    out = prices.load(["AAA", "BBB", "CCC"], refresh=True)
    assert calls[-1] == ["BBB"]  # retried
    for f, df in out.items():
        assert list(df.columns) == ["AAA", "BBB", "CCC"], f
    assert out["Close"]["BBB"].notna().all() and (out["Stock Splits"] == 0).all().all()
    # and the cached copy loads back without another download
    n = len(calls)
    again = prices.load(["AAA", "BBB", "CCC"])
    assert len(calls) == n and list(again["Stock Splits"].columns) == ["AAA", "BBB", "CCC"]


def test_cache_missing_a_ticker_in_one_field_redownloads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PRICE_CACHE", tmp_path)
    monkeypatch.setattr(db, "URL", "")
    full = pd.DataFrame(1.0, index=DAYS, columns=["AAA", "BBB"])
    for f in prices._FIELDS:
        (full if f != "Stock Splits" else full[["AAA"]]).to_csv(prices._cache_path(f))
    yf, calls = fake_yf(set())
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    out = prices.load(["AAA", "BBB"])  # used to raise KeyError: ['BBB'] not in index
    assert calls and list(out["Stock Splits"].columns) == ["AAA", "BBB"]

"""Step 4: point-in-time backtest of the screen.

Monthly rebalance. At each month-end, fundamentals are rebuilt from only the filings
public on that date, the same filters and scores as the live screen are applied, and
portfolios are held for one month (dividend-adjusted returns, equal weight).

Known bias, stated up front: the universe is *today's* index members. Names that were
dropped, acquired or went bust are missing, which flatters every strategy, and cheap
stocks most (value traps that died are exactly what's absent). Read results relative
to the equal-weight universe benchmark, which carries the same bias, not in absolute terms.
"""
from __future__ import annotations

import pickle

import numpy as np
import pandas as pd

from . import config, prices, score, screen, universe

COST_BPS = 10  # one-way, per unit of turnover
TOP_N = 25
IC_COLS = ["bargain", "blend", "value", "profitability", "balance_sheet", "growth", "price_action"]
XS_CACHE = config.DATA_DIR / "backtest_xs.pkl"


def month_ends(idx: pd.DatetimeIndex, start: str) -> list[pd.Timestamp]:
    s = pd.Series(idx, index=idx)
    me = s.groupby([idx.year, idx.month]).last()
    return [d for d in me if d >= pd.Timestamp(start)]


def build(start: str = "2017-02-01", reuse: bool = True, workers: int = 4) -> pd.DataFrame:
    if reuse and XS_CACHE.exists():
        return pd.read_pickle(XS_CACHE)
    uni = universe.load().set_index("ticker")
    px = prices.load(uni.index.tolist())
    dates = month_ends(px["Close"].index, start)
    print(f"[backtest] {len(dates)} rebalance dates x {len(uni)} tickers")
    xs = screen.cross_sections(dates, uni, px, workers=workers)
    xs.to_pickle(XS_CACHE)
    return xs


def _portfolio_returns(weights: dict[pd.Timestamp, pd.Series], fwd: pd.DataFrame) -> pd.Series:
    """Net monthly returns of an equal-weight book, with turnover costs."""
    out, prev = {}, pd.Series(dtype=float)
    for d, w in weights.items():
        if w.empty:
            out[d] = 0.0
            prev = w
            continue
        r = fwd.loc[d, w.index].fillna(0.0)
        turnover = w.subtract(prev, fill_value=0).abs().sum()
        out[d] = float((w * r).sum()) - turnover * COST_BPS / 1e4
        # Drift weights through the month for next turnover calc.
        grown = w * (1 + r)
        prev = grown / grown.sum() if grown.sum() else grown
    return pd.Series(out).sort_index()


def _ew(names) -> pd.Series:
    names = list(names)
    return pd.Series(1.0 / len(names), index=names) if names else pd.Series(dtype=float)


def stats(r: pd.Series, bench: pd.Series | None = None) -> dict:
    r = r.dropna()
    n = len(r)
    eq = (1 + r).cumprod()
    out = {
        "CAGR": eq.iloc[-1] ** (12 / n) - 1,
        "Vol": r.std() * np.sqrt(12),
        "Sharpe": r.mean() / r.std() * np.sqrt(12) if r.std() else np.nan,
        "MaxDD": (eq / eq.cummax() - 1).min(),
    }
    if bench is not None:
        b = bench.reindex(r.index)
        ex = r - b
        out["Excess/yr"] = (1 + r).prod() ** (12 / n) - (1 + b).prod() ** (12 / n)
        out["Hit rate"] = (ex > 0).mean()
        out["Excess t"] = ex.mean() / ex.std() * np.sqrt(n) if ex.std() else np.nan
    return out


def run(xs: pd.DataFrame, weights: dict | None = None) -> dict:
    uni = universe.load().set_index("ticker")
    px = prices.load(uni.index.tolist())
    adj = px["Adj Close"]
    dates = sorted(xs["date"].unique())
    # One-month forward return from each rebalance date to the next.
    a = adj.reindex(adj.index.union(dates)).ffill().loc[dates]
    fwd = (a.shift(-1) / a - 1).iloc[:-1]
    dates = list(fwd.index)

    books = {k: {} for k in ["Bargain top 25", "Blend top 25", "Value-only top 25", "All passers",
                             "Equal-weight universe"] + [f"Q{i}" for i in range(1, 6)]}
    ic_rows = []
    counts = []
    for d in dates:
        x = xs[xs["date"] == d].set_index("ticker")
        x = x.drop(columns=[c for c in ("ret_1m", "ret_ytd", "ret_1y", "off_high") if c in x])
        x = x.join(prices.performance(px, d))  # price-action bucket, as of d only
        s = score.score(x, weights, rank_by="bargain")
        inv = s[s["investable"]]
        ok = inv[inv["passes"]]
        counts.append({"date": d, "investable": len(inv), "passers": len(ok)})
        books["Bargain top 25"][d] = _ew(ok.nlargest(TOP_N, "bargain").index)
        books["Blend top 25"][d] = _ew(ok.nlargest(TOP_N, "blend").index)
        books["All passers"][d] = _ew(ok.index)
        books["Value-only top 25"][d] = _ew(ok.nlargest(TOP_N, "value").index)
        books["Equal-weight universe"][d] = _ew(inv.index)
        q = pd.qcut(inv["bargain"].rank(method="first"), 5, labels=False) + 1
        for i in range(1, 6):
            books[f"Q{i}"][d] = _ew(q[q == i].index)
        f = fwd.loc[d].reindex(inv.index)
        row = {"date": d}
        for c in IC_COLS:
            m = inv[c].notna() & f.notna()
            row[c] = inv.loc[m, c].rank().corr(f[m].rank()) if m.sum() > 30 else np.nan
        ic_rows.append(row)

    rets = pd.DataFrame({k: _portfolio_returns(v, fwd) for k, v in books.items()})
    spy = prices.load(["SPY"], name="bench_")["Adj Close"]["SPY"]
    sp = spy.reindex(spy.index.union(dates)).ffill().loc[list(fwd.index) + [a.index[-1]]]
    rets["SPY"] = (sp.shift(-1) / sp - 1).iloc[:-1].values
    bench = rets["Equal-weight universe"]
    table = pd.DataFrame({k: stats(rets[k], None if k == "Equal-weight universe" else bench)
                          for k in rets.columns}).T
    half = len(rets) // 2
    halves = pd.DataFrame({
        f"{rets.index[0]:%Y-%m}–{rets.index[half - 1]:%Y-%m}":
            {k: stats(rets[k].iloc[:half], bench.iloc[:half]).get("Excess/yr") for k in books
             if k != "Equal-weight universe"},
        f"{rets.index[half]:%Y-%m}–{rets.index[-1]:%Y-%m}":
            {k: stats(rets[k].iloc[half:], bench.iloc[half:]).get("Excess/yr") for k in books
             if k != "Equal-weight universe"},
    })
    ic = pd.DataFrame(ic_rows).set_index("date")
    ic_summary = pd.DataFrame({"mean IC": ic.mean(), "t-stat": ic.mean() / ic.std() * np.sqrt(ic.count()),
                               "% months > 0": (ic > 0).mean()})
    return {"returns": rets, "table": table, "halves": halves, "ic": ic,
            "ic_summary": ic_summary, "counts": pd.DataFrame(counts).set_index("date"),
            "holdings": books["Bargain top 25"]}

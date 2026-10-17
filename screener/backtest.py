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
IC_COLS = ["implied_return", "cash_yield", "growth_rate", "bargain", "value", "profitability",
           "balance_sheet", "growth", "mom_12_1", "price_action"]
XS_CACHE = config.DATA_DIR / "backtest_xs.pkl"


def month_ends(idx: pd.DatetimeIndex, start: str) -> list[pd.Timestamp]:
    s = pd.Series(idx, index=idx)
    me = s.groupby([idx.year, idx.month]).last()
    return [d for d in me if d >= pd.Timestamp(start)]


def build(start: str = "2017-02-01", reuse: bool = True, workers: int = 4) -> pd.DataFrame:
    if reuse and XS_CACHE.exists():
        return pd.read_pickle(XS_CACHE)
    if reuse:
        from . import store
        store.pull()
        xs = store.load_history()
        if xs is not None:
            xs.to_pickle(XS_CACHE)
            return xs
    uni = universe.load().set_index("ticker")
    px = prices.load(uni.index.tolist())
    # The screen is capped at $5B: large caps never qualify and aren't peers, so skip the
    # S&P 500 / Nasdaq-100-only names (saves ~40% of the rebuild).
    uni = uni[uni["index"].str.contains("SP600|SP400")]
    dates = month_ends(px["Close"].index, start)
    print(f"[backtest] {len(dates)} rebalance dates x {len(uni)} tickers")
    xs = screen.cross_sections(dates, uni, px, workers=workers)
    xs.to_pickle(XS_CACHE)
    from . import store
    store.save_history(xs)
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


STRATEGIES = [
    "Implied return top 25",
    "Implied return + momentum gate",
    "Implied return, no P/S rule",
    "Bargain top 25",
    "Baseline: top 25 FCF yield",
    "All gated names",
    "Equal-weight universe",
]
HOLDS = (1, 3, 12)
MOM_CUT = 0.2  # momentum gate drops the worst 20% of 12-1 month performers


def _overlap(books: dict, h: int) -> dict:
    """Hold each monthly pick for h months: weights are the mean of the last h cohorts
    (Jegadeesh-Titman overlapping portfolios, 1/h of capital rebalanced each month)."""
    dates = list(books)
    out = {}
    for i, d in enumerate(dates):
        cohorts = [books[x] for x in dates[max(0, i - h + 1):i + 1]]
        w = pd.concat(cohorts, axis=1).fillna(0).sum(axis=1) / len(cohorts) if cohorts else pd.Series(dtype=float)
        out[d] = w[w > 0]
    return out


def run(xs: pd.DataFrame, weights: dict | None = None) -> dict:
    uni = universe.load().set_index("ticker")
    px = prices.load(uni.index.tolist())
    adj = px["Adj Close"]
    dates = sorted(xs["date"].unique())
    a = adj.reindex(adj.index.union(dates)).ffill().loc[dates]
    fwd = (a.shift(-1) / a - 1).iloc[:-1]
    dates = list(fwd.index)

    books = {k: {} for k in STRATEGIES + [f"Q{i}" for i in range(1, 6)]}
    ic_rows, counts = [], []
    for d in dates:
        x = xs[xs["date"] == d].set_index("ticker")
        x = x.drop(columns=[c for c in ("ret_1m", "ret_ytd", "ret_1y", "off_high", "mom_12_1") if c in x])
        x = x.join(prices.performance(px, d))
        x["sector"] = uni["sector"].reindex(x.index).fillna(x["sector"])  # current overrides
        x["us_hq"] = uni["us_hq"].reindex(x.index).fillna(False)
        s = score.score(x, weights, rank_by="implied_return")
        inv = s[s["investable"] & ~s["is_financial"].astype(bool)]
        ok = inv[inv["passes"]]
        f = score.filters(x.loc[inv.index])
        ok_nops = inv[f.drop(columns="price_to_sales").all(axis=1)]
        mom_floor = inv["mom_12_1"].quantile(MOM_CUT)
        counts.append({"date": d, "investable": len(inv), "passers": len(ok)})
        books["Implied return top 25"][d] = _ew(score.top_n(ok, "implied_return", TOP_N).index)
        books["Implied return + momentum gate"][d] = _ew(
            score.top_n(ok[ok["mom_12_1"] > mom_floor], "implied_return", TOP_N).index)
        books["Implied return, no P/S rule"][d] = _ew(score.top_n(ok_nops, "implied_return", TOP_N).index)
        books["Bargain top 25"][d] = _ew(score.top_n(ok, "bargain", TOP_N).index)
        books["Baseline: top 25 FCF yield"][d] = _ew(inv.nlargest(TOP_N, "fcf_yield").index)
        books["All gated names"][d] = _ew(ok.index)
        books["Equal-weight universe"][d] = _ew(inv.index)
        q = pd.qcut(inv["implied_return"].rank(method="first"), 5, labels=False) + 1
        for i in range(1, 6):
            books[f"Q{i}"][d] = _ew(q[q == i].index)
        f = fwd.loc[d].reindex(inv.index)
        row = {"date": d}
        for c in IC_COLS:
            m = inv[c].notna() & f.notna()
            row[c] = inv.loc[m, c].rank().corr(f[m].rank()) if m.sum() > 30 else np.nan
        ic_rows.append(row)

    spy = prices.load(["SPY"], name="bench_")["Adj Close"]["SPY"]
    sp = spy.reindex(spy.index.union(a.index)).ffill().loc[a.index]
    spy_r = (sp.shift(-1) / sp - 1).iloc[:-1]

    by_hold, tables = {}, {}
    for h in HOLDS:
        rets = pd.DataFrame({k: _portfolio_returns(_overlap(v, h), fwd) for k, v in books.items()})
        # Skip the warm-up months where fewer than h cohorts exist.
        rets = rets.iloc[h - 1:]
        rets["SPY"] = spy_r.reindex(rets.index).values
        bench = rets["Equal-weight universe"]
        by_hold[h] = rets
        tables[h] = pd.DataFrame({k: stats(rets[k], None if k == "Equal-weight universe" else bench)
                                  for k in rets.columns}).T
    rets = by_hold[3]
    bench = rets["Equal-weight universe"]
    half = len(rets) // 2
    halves = pd.DataFrame({
        f"{rets.index[0]:%Y-%m}–{rets.index[half - 1]:%Y-%m}":
            {k: stats(rets[k].iloc[:half], bench.iloc[:half]).get("Excess/yr") for k in STRATEGIES[:-1]},
        f"{rets.index[half]:%Y-%m}–{rets.index[-1]:%Y-%m}":
            {k: stats(rets[k].iloc[half:], bench.iloc[half:]).get("Excess/yr") for k in STRATEGIES[:-1]},
    })
    ic = pd.DataFrame(ic_rows).set_index("date")
    ic_summary = pd.DataFrame({"mean IC": ic.mean(), "t-stat": ic.mean() / ic.std() * np.sqrt(ic.count()),
                               "% months > 0": (ic > 0).mean()})
    return {"returns": rets, "by_hold": by_hold, "tables": tables, "table": tables[3],
            "halves": halves, "ic": ic, "ic_summary": ic_summary,
            "counts": pd.DataFrame(counts).set_index("date"),
            "holdings": books["Implied return + momentum gate"]}

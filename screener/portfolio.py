"""Forward scorecard: the monthly top 25 held as an equal-weight paper portfolio.

The backtest carries survivorship bias (today's index members); this does not. Each month's
first screen opens a book at that run's prices; it is held until the next book opens, and
books chain into one track record compared with SPY (S&P 500), IJR (S&P 600) and IJH (S&P 400).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import db, score

N = 25
BENCH = {"SPY": "S&P 500", "IJR": "S&P 600", "IJH": "S&P 400"}


def open_book(scored: pd.DataFrame, start=None, force: bool = False) -> int:
    """Open this month's book from a scored screen, unless one is already open this month."""
    start = pd.Timestamp(start if start is not None else scored["date"].iloc[0]).normalize()
    with db.connect() as con:
        last = con.execute("select max(start) from books").fetchone()[0]
        if last is not None and not force and (last.year, last.month) == (start.year, start.month):
            return 0
        p = scored[scored["passes"] & ~scored["is_financial"].astype(bool)]
        top = score.top_n(p, "composite" if "composite" in p else "implied_return", N)
        rows = [(start.date(), t, 1.0 / len(top), float(top.at[t, "price"]), i + 1, _f(top.at[t, "implied_return"]))
                for i, t in enumerate(top.index)]
        with con.transaction():
            con.execute("delete from books where start = %s", (start.date(),))
            db.copy_rows(con, "books", ["start", "ticker", "weight", "entry_price", "rank", "implied_return"], rows)
    return len(rows)


def _f(v):
    return None if v is None or pd.isna(v) else float(v)


def scorecard() -> dict:
    from . import warehouse
    books = db.read("select * from books order by start, rank", parse_dates=["start"])
    if books.empty:
        return {"books": [], "series": [], "summary": {}}
    starts = sorted(books["start"].unique())
    px = warehouse.load_prices(sorted(set(books["ticker"]) | set(BENCH)), start=starts[0] - pd.Timedelta(days=7))
    adj, close = px["Adj Close"].ffill(), px["Close"].ffill()
    days = adj.index[adj.index >= starts[0]]
    value = [1.0]
    out_books = []
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else days[-1]
        b = books[books["start"] == s].set_index("ticker")
        seg = days[(days >= s) & (days <= end)]
        if len(seg) == 0:
            continue
        # Entry at the screen's own price; after that, total return (dividends in).
        first = close.loc[seg[0], b.index]
        entry_adj = adj.loc[seg[0], b.index] * (b["entry_price"] / first)
        rel = adj.loc[seg, b.index].div(entry_adj)  # growth of $1 in each name
        port = (rel * b["weight"]).sum(axis=1) / b["weight"].sum()
        base = value[-1]
        seg_vals = list(base * port.values)
        value.extend(seg_vals[1:])
        last = rel.iloc[-1]
        out_books.append({"start": f"{s:%Y-%m-%d}", "end": f"{seg[-1]:%Y-%m-%d}",
                          "ret": float(port.iloc[-1] - 1),
                          "bench": {k: float(adj.loc[seg[-1], k] / adj.loc[seg[0], k] - 1) for k in BENCH},
                          "names": [{"t": t, "rank": int(b.at[t, "rank"]), "entry": _f(b.at[t, "entry_price"]),
                                     "ret": _f(last.get(t) - 1 if pd.notna(last.get(t)) else None)} for t in b.index]})
    idx = days[:len(value)]
    bench = {k: list(adj.loc[idx, k] / adj.loc[idx[0], k]) for k in BENCH}
    series = [{"d": f"{d:%Y-%m-%d}", "v": round(float(v), 5), **{k: round(float(bench[k][i]), 5) for k in BENCH if i < len(bench[k])}}
              for i, (d, v) in enumerate(zip(idx, value))]
    tot = value[-1] - 1
    summary = {"since": f"{starts[0]:%Y-%m-%d}", "days": int((idx[-1] - idx[0]).days), "return": float(tot),
               **{f"vs_{k}": float(tot - (bench[k][-1] - 1)) for k in BENCH},
               "hit_rate": float(np.mean([x["ret"] > x["bench"]["IJR"] for x in out_books])) if out_books else None}
    return {"books": out_books, "series": series, "summary": summary, "bench": BENCH}

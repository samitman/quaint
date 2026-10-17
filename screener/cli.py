"""Command line entry point.

    python -m screener universe          # rebuild index membership + CIK map
    python -m screener fetch             # pull/refresh EDGAR facts and prices
    python -m screener check AAPL KO     # print the derived fundamentals for a few tickers
"""
from __future__ import annotations

import argparse

import pandas as pd

from . import edgar, fundamentals, prices, universe


def cmd_universe(a):
    uni = universe.build()
    print(uni.groupby("exchange").size().to_string())
    print(f"{len(uni)} tickers, {uni['cik'].nunique()} companies")


def cmd_fetch(a):
    uni = universe.build() if a.rebuild else universe.load()
    edgar.fetch_many(uni["cik"], refresh=a.refresh)
    prices.load(uni["ticker"].tolist(), refresh=a.refresh)
    print("done")


def cmd_check(a):
    uni = universe.load().set_index("ticker")
    keys = ["period_end", "revenue_ttm", "revenue_ttm_prev", "net_income_ttm", "cfo_ttm",
            "capex_ttm", "operating_income_ttm", "dna_ttm", "equity", "total_debt", "cash",
            "st_investments", "diluted_shares", "shares_out_dei"]
    rows = {}
    for t in a.tickers:
        t = universe.norm_ticker(t)
        if t not in uni.index:
            print(f"{t}: not in universe")
            continue
        cf = edgar.fetch(int(uni.loc[t, "cik"]))
        v = fundamentals.snapshot(cf, as_of=a.as_of).values
        v["shares_used"] = fundamentals.shares_outstanding(v)
        rows[t] = {k: v.get(k) for k in keys + ["shares_used"]}
    pd.set_option("display.float_format", lambda x: f"{x:,.0f}")
    print(pd.DataFrame(rows).to_string())


def main(argv=None):
    p = argparse.ArgumentParser(prog="screener")
    sub = p.add_subparsers(required=True)
    s = sub.add_parser("universe"); s.set_defaults(fn=cmd_universe)
    s = sub.add_parser("fetch"); s.set_defaults(fn=cmd_fetch)
    s.add_argument("--refresh", action="store_true", help="ignore caches")
    s.add_argument("--rebuild", action="store_true", help="rebuild the universe first")
    s = sub.add_parser("check"); s.set_defaults(fn=cmd_check)
    s.add_argument("tickers", nargs="+")
    s.add_argument("--as-of", type=pd.Timestamp, default=None)
    a = p.parse_args(argv)
    a.fn(a)

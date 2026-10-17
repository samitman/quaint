"""Command line entry point.

    python -m screener universe          # rebuild index membership + CIK map
    python -m screener fetch             # pull/refresh EDGAR facts and prices
    python -m screener check AAPL KO     # print the derived fundamentals for a few tickers
    python -m screener screen            # live ranked report -> reports/screen_<date>.html/.csv
    python -m screener backtest          # point-in-time monthly backtest -> reports/backtest.html
    python -m screener lookup PLTR KO    # one stock's rank and breakdown, in the universe or not
    python -m screener audit             # data-quality flags across the universe
"""
from __future__ import annotations

import argparse

import pandas as pd

from pathlib import Path

from . import config, edgar, fundamentals, prices, universe


def cmd_universe(a):
    uni = universe.build()
    print(uni.groupby("exchange").size().to_string())
    print(f"{len(uni)} tickers, {uni['cik'].nunique()} companies")


def cmd_fetch(a):
    from . import store
    if not config.EDGAR_CACHE.exists() or not any(config.EDGAR_CACHE.iterdir()):
        store.pull()  # seed a fresh session from the shared db/
    uni = universe.build() if a.rebuild else universe.load()
    edgar.fetch_many(uni["cik"], refresh=a.refresh)
    prices.load(uni["ticker"].tolist(), refresh=a.refresh)
    store.push()
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


REPORTS = config.ROOT / "reports"


def _weights(a):
    from . import score
    score.RANK_BY = a.rank_by
    for attr, arg in (("MIN_MCAP", "min_mcap"), ("MAX_MCAP", "max_mcap"), ("MAX_PS", "max_ps"),
                      ("MIN_NET_MARGIN", "min_net_margin"), ("MAX_ND_EBITDA", "max_nd_ebitda"),
                      ("MIN_ADV", "min_adv")):
        setattr(score, attr, getattr(a, arg))
    score.US_ONLY = not a.include_foreign
    score.PS_VS_SECTOR = a.ps_vs_sector
    return {"value": a.w_value, "growth": a.w_growth, "profitability": a.w_profitability,
            "balance_sheet": a.w_balance, "price_action": a.w_price}


def cmd_screen(a):
    from . import report, screen
    from . import yahoo_fill
    s = screen.run(as_of=a.as_of, weights=_weights(a))
    p = s[s["passes"]]
    shown = (p[~p["is_financial"].astype(bool)].head(a.top).index.tolist()
             + p[p["is_financial"].astype(bool)].head(10).index.tolist())
    s = yahoo_fill.crosscheck(s, shown)
    htm, csv = report.write(s, Path(a.out), top=a.top)
    from . import store
    store.save_screen(s, report.CSV_COLS)
    store.push()
    p = s[s["passes"]]
    print(p[["sec_name", "sector", "pe", "pb", "fcf_yield", "rev_growth", "bargain", "implied_return"]]
          .head(a.top).to_string(float_format=lambda x: f"{x:.2f}"))
    print(f"{len(p)} pass / {int(s['investable'].sum())} investable -> {htm}")


def cmd_backtest(a):
    from . import backtest, bt_report
    xs = backtest.build(start=a.start, reuse=not a.rebuild)
    from . import store
    store.push()
    res = backtest.run(xs, weights=_weights(a))
    print(res["table"].to_string(float_format=lambda x: f"{x:.3f}"))
    print(res["ic_summary"].to_string(float_format=lambda x: f"{x:.3f}"))
    print(bt_report.write(res, Path(a.out)))


def cmd_lookup(a):
    from . import lookup
    for t in a.tickers:
        print(lookup.lookup(t, weights=_weights(a)))
        print()


def cmd_audit(a):
    from . import screen
    s = screen.run()
    inv = s[s["investable"]]
    print(f"investable {len(inv)} of {len(s)}")
    for col in ("revenue", "net_income", "equity", "ebit", "fcf", "fscore", "shares"):
        miss = inv[inv[col].isna()].index.tolist()
        print(f"missing {col}: {len(miss)} {miss[:20]}")
    print("stale data (>200d):", s[s["data_age_days"] > 200].index.tolist())
    print("eps-implied/class shares:", s[~s["shares_source"].isin(["cover", "diluted"])]["shares_source"].to_dict())
    print("debt from last 10-K only:", s[s["debt_stale"]].index.tolist()[:40])
    print("no debt tags at all:", s[~s["debt_tagged"]].index.tolist()[:40])


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
    for name, fn in (("screen", cmd_screen), ("backtest", cmd_backtest), ("audit", cmd_audit),
                     ("lookup", cmd_lookup)):
        s = sub.add_parser(name); s.set_defaults(fn=fn)
        if name == "lookup":
            s.add_argument("tickers", nargs="+")
        s.add_argument("--rank-by", choices=["implied_return", "bargain", "blend"],
                       default="implied_return",
                       help="implied_return: cash yield + growth; bargain: cheap vs. what "
                            "fundamentals justify (sector-relative); blend: weighted scores")
        s.add_argument("--min-mcap", type=float, default=300e6)
        s.add_argument("--max-mcap", type=float, default=5e9)
        s.add_argument("--max-ps", type=float, default=None, help="optional ceiling on price/sales")
        s.add_argument("--ps-vs-sector", action="store_true",
                       help="also require P/S below the sector median")
        s.add_argument("--min-net-margin", type=float, default=0.05)
        s.add_argument("--max-nd-ebitda", type=float, default=1.0)
        s.add_argument("--min-adv", type=float, default=2e6, help="min median daily $ volume")
        s.add_argument("--include-foreign", action="store_true", help="allow non-US HQ")
        s.add_argument("--w-value", type=float, default=0.40)
        s.add_argument("--w-growth", type=float, default=0.25)
        s.add_argument("--w-profitability", type=float, default=0.20)
        s.add_argument("--w-balance", type=float, default=0.15)
        s.add_argument("--w-price", type=float, default=0.0)
        s.add_argument("--out", default=str(REPORTS))
        if name == "screen":
            s.add_argument("--top", type=int, default=25)
            s.add_argument("--as-of", type=pd.Timestamp, default=None)
        if name == "backtest":
            s.add_argument("--start", default="2017-02-01")
            s.add_argument("--rebuild", action="store_true", help="recompute point-in-time history")
    a = p.parse_args(argv)
    a.fn(a)

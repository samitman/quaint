"""Command line entry point.

    python -m screener universe          # rebuild index membership + CIK map
    python -m screener fetch             # pull/refresh EDGAR facts and prices
    python -m screener check AAPL KO     # print the derived fundamentals for a few tickers
    python -m screener screen            # live ranked report -> reports/screen_<date>.html/.csv
    python -m screener backtest          # point-in-time monthly backtest -> reports/backtest.html
    python -m screener lookup PLTR KO    # one stock's rank and breakdown, in the universe or not
    python -m screener audit             # data-quality flags across the universe
    python -m screener sync              # load everything into Postgres (DATABASE_URL)
    python -m screener refresh           # nightly: new filings + prices, then a screen run
    python -m screener worker            # run `refresh` every weekday evening
    python -m screener app               # QUAINT web app on http://localhost:8765
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
                      ("MIN_FSCORE", "min_fscore"),
                      ("MIN_ADV", "min_adv")):
        setattr(score, attr, getattr(a, arg))
    score.US_ONLY = not a.include_foreign
    score.PS_VS_SECTOR = a.ps_vs_sector
    score.PROFITS_GROWING = a.profit_growth
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
    from . import events, score
    core = p[~p["is_financial"].astype(bool)]
    news = {t: events.headlines(str(s.at[t, "sec_name"]), t) for t in score.top_n(core, "composite", a.top).index}
    htm, csv = report.write(s, Path(a.out), top=a.top, news=news)
    from . import explorer
    print(explorer.write(s, Path(a.out), news=news))
    from . import appdata
    print(appdata.build(s, news))
    from . import db, store
    store.save_screen(s, report.CSV_COLS)
    if db.enabled():
        from . import insider, portfolio, warehouse
        print(f"[db] screen run {warehouse.record_screen(s, news)}; book: {portfolio.open_book(s) or 'already open this month'}")
        if insider.TRADES.exists():
            warehouse.sync_trades(insider.build())
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


def cmd_explorer(a):
    """Rebuild the explorer page from the last live screen (no new prices), with fresh headlines."""
    import pandas as pd
    from . import events, explorer, score, screen
    s = score.score(pd.read_pickle(screen.LATEST).set_index("ticker"), _weights(a))
    core = s[s["passes"] & ~s["is_financial"].astype(bool)]
    uni_name = s["sec_name"] if "sec_name" in s else pd.Series(dtype=str)
    news = {t: events.headlines(str(uni_name.get(t, t)), t) for t in score.top_n(core, "composite", a.top).index}
    print(explorer.write(s, Path(a.out), news=news))


BENCH = ["SPY", "IJR", "IJH", "IWM"]  # S&P 500, S&P 600, S&P 400, Russell 2000


def cmd_sync(a):
    """Load (or top up) the Postgres warehouse from our caches and SEC."""
    from . import db, insider, warehouse
    db.init()
    uni = universe.load()
    warehouse.sync_companies(uni)
    print(f"[sync] {len(uni)} companies")
    if not a.skip_prices:
        px = prices.load(uni["ticker"].tolist())
        n = warehouse.sync_prices(px)
        n += warehouse.sync_prices(prices.load(BENCH, name="bench_"))
        for k in ("universe", "bench_"):
            db.set_meta(f"prices_refreshed:{k}", pd.Timestamp.now(tz="UTC").isoformat())
        print(f"[sync] {n:,} price rows")
    if insider.TRADES.exists():
        t = insider.build()
        warehouse.sync_trades(t)
        print(f"[sync] {len(t):,} insider trades")
    if not a.skip_filings:
        warehouse.sync_fundamentals(uni["cik"], workers=a.workers, rebuild=a.rebuild)


def cmd_app(a):
    from . import server
    if a.build:
        print(server.build_from_latest())
    server.serve(a.port, open_browser=not a.no_browser, host=a.host)


def cmd_refresh(a):
    """Nightly: new prices and filings into the warehouse, then a full screen run."""
    from . import db
    db.init()
    with db.connect() as lock:  # one refresh at a time (worker and a manual run can overlap)
        if not lock.execute("select pg_try_advisory_lock(7231)").fetchone()[0]:
            print("[refresh] another refresh is already running; skipping")
            return
        _refresh(a)


def _refresh(a):
    from . import db, warehouse
    import time
    f = config.UNIVERSE_FILE
    if not f.exists() or time.time() - f.stat().st_mtime > 30 * 86400:  # index changes are quarterly
        universe.build()
    uni = universe.load()
    warehouse.sync_companies(uni)
    edgar.fetch_many(uni["cik"])  # re-downloads companyfacts older than a week
    stale = warehouse.stale_ciks(uni["cik"])
    print(f"[refresh] {len(stale)} companies with new filings")
    warehouse.sync_fundamentals(stale, workers=a.workers)
    prices.load(BENCH, name="bench_", refresh=True)
    cmd_screen(a)
    db.set_meta("last_refresh", pd.Timestamp.now(tz="UTC").isoformat())


def _last_due(now: pd.Timestamp, hh: int, mm: int) -> pd.Timestamp:
    """The most recent weekday refresh time at or before `now`."""
    due = now.normalize() + pd.Timedelta(hours=hh, minutes=mm)
    if due > now:
        due -= pd.Timedelta(days=1)
    while due.weekday() >= 5:
        due -= pd.Timedelta(days=1)
    return due


def cmd_worker(a):
    """Refresh after every weekday's US close, forever. Checks every 10 minutes whether a
    refresh is due, so a laptop that slept through the evening catches up when it wakes."""
    import time
    from zoneinfo import ZoneInfo
    from . import db
    tz = ZoneInfo("America/New_York")
    hh, mm = (int(x) for x in a.at.split(":"))
    db.init()
    tried = None
    while True:
        now = pd.Timestamp.now(tz=tz)
        due = _last_due(now, hh, mm)
        last = db.get_meta("last_refresh")
        done = last is not None and pd.Timestamp(last) >= due
        if (a.now or not done) and (tried is None or now - tried > pd.Timedelta(hours=1)):
            a.now, tried = False, now  # a failed run retries an hour later, not in a tight loop
            print(f"[worker] refreshing (due {due:%a %H:%M %Z})", flush=True)
            try:
                cmd_refresh(a)
            except Exception as e:
                print(f"[worker] refresh failed: {e!r}", flush=True)
            continue
        time.sleep(600)


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
    s = sub.add_parser("sync", help="load our data into the Postgres warehouse (DATABASE_URL)")
    s.set_defaults(fn=cmd_sync)
    s.add_argument("--rebuild", action="store_true", help="recompute every point-in-time version")
    s.add_argument("--skip-prices", action="store_true")
    s.add_argument("--skip-filings", action="store_true")
    s.add_argument("--workers", type=int, default=4)
    s = sub.add_parser("app", help="run the research app locally"); s.set_defaults(fn=cmd_app)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--build", action="store_true", help="rebuild app data from the last screen first")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve other machines (set QUAINT_PASSWORD)")
    for name, fn in (("screen", cmd_screen), ("backtest", cmd_backtest), ("audit", cmd_audit),
                     ("lookup", cmd_lookup), ("explorer", cmd_explorer), ("refresh", cmd_refresh),
                     ("worker", cmd_worker)):
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
        s.add_argument("--min-fscore", type=float, default=None, help="optional Piotroski floor (e.g. 6)")
        s.add_argument("--min-adv", type=float, default=2e6, help="min median daily $ volume")
        s.add_argument("--include-foreign", action="store_true", help="allow non-US HQ")
        s.add_argument("--w-value", type=float, default=0.40)
        s.add_argument("--w-growth", type=float, default=0.25)
        s.add_argument("--w-profitability", type=float, default=0.20)
        s.add_argument("--w-balance", type=float, default=0.15)
        s.add_argument("--w-price", type=float, default=0.0)
        s.add_argument("--out", default=str(REPORTS))
        s.add_argument("--profit-growth", action="store_true",
                       help="require profit per share above 3 years ago (optional gate)")
        if name in ("screen", "explorer", "refresh", "worker"):
            s.add_argument("--top", type=int, default=25)
        if name in ("screen", "refresh", "worker"):
            s.add_argument("--as-of", type=pd.Timestamp, default=None)
        if name in ("refresh", "worker"):
            s.add_argument("--workers", type=int, default=4)
        if name == "worker":
            s.add_argument("--at", default="18:30", help="New York time to refresh on weekdays")
            s.add_argument("--now", action="store_true", help="also refresh once at start")
        if name == "backtest":
            s.add_argument("--start", default="2017-02-01")
            s.add_argument("--rebuild", action="store_true", help="recompute point-in-time history")
    a = p.parse_args(argv)
    a.fn(a)

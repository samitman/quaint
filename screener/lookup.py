"""Single-stock breakdown: where a ticker ranks, and why.

Works for any SEC-registered ticker. Names outside the universe are pulled on demand,
given a GICS-style sector (from Yahoo), and scored against the latest screen's sector
peers, so the score means the same thing as for names in the report.
"""
from __future__ import annotations

import pandas as pd

from . import edgar, fundamentals, metrics, prices, score, screen, universe, yahoo_fill

# Yahoo's sector names -> GICS sector names used by the universe.
YAHOO_TO_GICS = {
    "Technology": "Information Technology", "Financial Services": "Financials",
    "Healthcare": "Health Care", "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples", "Basic Materials": "Materials",
    "Communication Services": "Communication Services", "Industrials": "Industrials",
    "Energy": "Energy", "Utilities": "Utilities", "Real Estate": "Real Estate",
}

ROWS = [  # (metric, label, kind) shown with the sector percentile where one exists
    ("price", "Price", "$"), ("mcap", "Market cap", "B"), ("pe", "P/E", "x"), ("pb", "P/B", "x"),
    ("ev_ebit", "EV/EBIT", "x"), ("earnings_yield", "Earnings yield", "%"),
    ("fcf_yield", "FCF yield", "%"), ("ebit_ev", "EBIT/EV", "%"), ("book_yield", "Book/price", "%"),
    ("roic", "ROIC", "%"), ("roe", "ROE", "%"), ("op_margin", "Operating margin", "%"),
    ("fcf_conversion", "FCF / net income", "%"), ("rev_growth", "Revenue growth (TTM y/y)", "%"),
    ("rev_cagr_3y", "Revenue CAGR 3y", "%"), ("ni_growth", "Net income growth", "%"),
    ("margin_change", "Op margin change (pp)", "pp"), ("net_income", "Net income TTM", "B"),
    ("fcf", "Free cash flow TTM", "B"), ("nd_ebitda", "Net debt / EBITDA", "x"),
    ("total_debt", "Debt", "B"), ("debt_yoy", "Debt change y/y", "%"),
    ("fscore", "Piotroski F (0-9)", "f"),
    ("ret_1m", "Return 1 month", "%"), ("ret_ytd", "Return YTD", "%"),
    ("ret_1y", "Return 1 year", "%"), ("off_high", "From 52-week high", "%"),
]


def _external_row(ticker: str, latest: pd.DataFrame) -> pd.DataFrame:
    sec = universe.sec_ticker_map().set_index("ticker")
    if ticker not in sec.index:
        raise SystemExit(f"{ticker}: not an SEC-registered ticker (foreign/OTC names lack XBRL).")
    import yfinance as yf
    info = yf.Ticker(ticker).info
    sector = YAHOO_TO_GICS.get(info.get("sector", ""), info.get("sector", "Unknown"))
    cik = int(sec.loc[ticker, "cik"] if not isinstance(sec.loc[ticker], pd.DataFrame)
              else sec.loc[ticker, "cik"].iloc[0])
    cf = edgar.fetch(cik)
    if not cf:
        raise SystemExit(f"{ticker}: SEC has no XBRL financials for CIK {cik}.")
    px = prices.update_today(prices.load([ticker], name=f"single_{ticker}_"))
    d = pd.Timestamp(latest["date"].iloc[0])
    close = px["Close"][ticker][:d].dropna()
    snap = fundamentals.snapshot(cf, as_of=d).values
    splits = px["Stock Splits"][ticker].fillna(0)
    m = metrics.compute(snap, ticker, sector, float(close.iloc[-1]), as_of=d, splits=splits,
                            industry=info.get("industry", ""))
    used = yahoo_fill.patch(snap, ticker, d, m["debt_estimated"])
    if used:
        m = metrics.compute(snap, ticker, sector, float(close.iloc[-1]), as_of=d, splits=splits,
                            industry=info.get("industry", ""))
    m["yahoo_fields"] = ",".join(used)
    m.update(date=d, industry=info.get("industry", ""), sec_name=info.get("longName", ticker),
             index="not in index", exchange=sec.loc[ticker, "exchange"] if "exchange" in sec else "")
    adv = (px["Close"][ticker] * px["Volume"][ticker]).rolling(63, min_periods=40).median()
    m["adv_usd"] = adv.asof(d)
    hq = universe.hq_location(cik)
    m["us_hq"] = bool(len(hq) == 2 and hq.isalpha() and hq not in universe._NON_US_CODES)
    m.update(prices.performance(px, d).loc[ticker].to_dict())
    row = pd.DataFrame([m])
    screen._fill_missing_shares(row)
    return row


def lookup(ticker: str, weights: dict | None = None) -> str:
    ticker = universe.norm_ticker(ticker)
    if not screen.LATEST.exists():
        screen.run(weights=weights)
    latest = pd.read_pickle(screen.LATEST)
    in_uni = ticker in set(latest["ticker"])
    xs = latest if in_uni else pd.concat([latest, _external_row(ticker, latest)], ignore_index=True)
    s = score.score(xs.set_index("ticker"), weights)
    r = s.loc[ticker]
    passed = s[s["passes"]].sort_values("composite", ascending=False)
    peers = s[s["investable"] & (s["sector"] == r["sector"])]
    # Percentile of each metric vs. investable sector peers (100 = best for a bargain hunter).
    lines = [f"{ticker} · {r.get('sec_name', '')} · {r['sector']} / {r.get('industry', '')}",
             f"As of {pd.Timestamp(r['date']).date()}, fundamentals through {pd.Timestamp(r['period_end']).date()}"
             if pd.notna(r["period_end"]) else "No usable fundamentals",
             "In screener universe" if in_uni else "Not in universe: scored against the universe's sector peers"]
    if r["passes"]:
        rank = list(passed.index).index(ticker) + 1
        lines.append(f"PASSES all filters. Rank {rank} of {len(passed)} "
                     f"(implied return {100 * r['implied_return']:.1f}%, bargain {100 * r['bargain']:.0f}/100).")
    else:
        would = (passed["composite"] > r["composite"]).sum() + 1
        lines.append(f"FAILS: {r['failed']}. Implied return {100 * r['implied_return']:.1f}%, "
                     f"bargain {100 * r['bargain']:.0f}/100 "
                     f"(would rank about {would} of {len(passed)} if it passed).")
    pc = lambda k: "–" if pd.isna(r[k]) else f"{100 * r[k]:.0f}"
    lines.append(f"Valuation {pc('value')} · Profitability {pc('profitability')} · Balance sheet "
                 f"{pc('balance_sheet')} · Growth {pc('growth')} · Price (100=most beaten down) "
                 f"{pc('price_action')}   [sector percentiles, {len(peers)} peers]")
    lines.append("")
    better_high = {c for a, b in score.BUCKETS.values() for c in a + b if not c.startswith("-")}
    for c, label, kind in ROWS:
        v = r.get(c)
        txt = _fmt(v, kind)
        med = peers[c].median() if c in peers and peers[c].notna().any() else None
        extra = f"   sector median {_fmt(med, kind)}" if med is not None else ""
        if c in better_high and pd.notna(v) and peers[c].notna().sum() >= 5:
            extra += f"   pct {100 * (peers[c] < v).mean():.0f}"
        lines.append(f"  {label:<26}{txt:>10}{extra}")
    flags = []
    if r.get("debt_estimated"):
        flags.append("debt estimated from interest expense (no standard debt tags)")
    if r.get("debt_stale"):
        flags.append("debt taken from last 10-K (not tagged in 10-Qs)")
    if r.get("yahoo_fields"):
        flags.append(f"from Yahoo (EDGAR gap): {r['yahoo_fields']}")
    if r.get("shares_source") not in ("cover", "diluted"):
        flags.append(f"share count source: {r.get('shares_source')}")
    if flags:
        lines += ["", "Data flags: " + "; ".join(flags)]
    return "\n".join(lines)


def _fmt(v, kind):
    if v is None or pd.isna(v):
        return "–"
    return {"$": f"{v:,.2f}", "B": f"{v / 1e9:,.1f}B", "x": f"{v:.1f}", "%": f"{100 * v:.1f}%",
            "pp": f"{100 * v:+.1f}", "f": f"{v:.1f}"}[kind]

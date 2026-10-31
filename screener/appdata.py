"""Data behind the research app (screener/web): per-company history as JSON.

`build()` writes web/data/: index.json (every company's latest screen numbers, for search,
screens and compare) and one chunk file per first letter holding each company's quarterly
fundamentals, price history and insider trades. The same JSON is what the local server
returns for tickers outside the universe (`company()`).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, fundamentals as F

OUT = Path(__file__).resolve().parent.parent / "web" / "data"
QUARTERS = 44  # ~11 years of quarterly history
FLOWS = ["revenue", "gross_profit", "operating_income", "pretax_income", "income_tax", "net_income",
         "cfo", "capex", "sbc", "dna", "interest_expense", "buybacks", "dividends", "acquisitions",
         "net_interest_income", "noninterest_income"]
INSTANTS = ["cash", "st_investments", "equity", "assets", "lt_debt_noncurrent", "lt_debt_current",
            "lt_debt_total", "st_debt", "debt_current"]


def _num(v, sig=6):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return float(f"{v:.{sig}g}")


def _ttm(q: pd.Series) -> pd.Series:
    """Rolling sum of four contiguous quarters (None where a quarter is missing)."""
    out = {}
    idx = list(q.index)
    for i in range(3, len(idx)):
        span = (idx[i] - idx[i - 3]).days
        if 230 <= span <= 310:
            out[idx[i]] = float(q.iloc[i - 3:i + 1].sum())
    return pd.Series(out, dtype=float)


def _align(s: pd.Series, idx: pd.DatetimeIndex, tol_days: int = 12) -> pd.Series:
    if s is None or s.empty:
        return pd.Series(np.nan, index=idx)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.reindex(idx, method="nearest", tolerance=pd.Timedelta(days=tol_days))


def quarterly(cf: dict) -> pd.DataFrame:
    """Quarter-end rows: TTM flows, balance sheet, shares. Latest filings win (restated)."""
    q = {}
    for name in FLOWS + ["diluted_shares"]:
        df = F._frame(cf, F.FLOW[name], None, duration=True, take_max=name in F._TAKE_MAX)
        q[name] = F.direct_quarters(df) if name in F._AVERAGES else F.discrete_quarters(df)
    ends = sorted(set(q["revenue"].index) | set(q["net_income"].index))
    if not ends:
        return pd.DataFrame()
    idx = pd.DatetimeIndex(ends)
    # Drop near-duplicate quarter ends (52/53-week calendars tag a few days apart).
    keep = [idx[0]] + [d for p, d in zip(idx, idx[1:]) if (d - p).days > 20]
    idx = pd.DatetimeIndex(keep)[-QUARTERS - 3:]
    out = pd.DataFrame(index=idx)
    for name in FLOWS:
        out[name] = _align(_ttm(q[name]), idx)
    if out["revenue"].isna().all():  # banks: net interest + non-interest income
        out["revenue"] = out["net_interest_income"] + out["noninterest_income"].fillna(0)
    # 10-Ks carry no fourth-quarter share average: fill single gaps from the neighbours.
    out["shares"] = _align(q["diluted_shares"], idx).interpolate(limit=1, limit_area="inside").ffill(limit=1)
    for name in INSTANTS:
        df = F._frame(cf, F.INSTANT[name], None, duration=False)
        s = df.set_index("end")["val"].astype(float) if len(df) else None
        out[name] = _align(s, idx, 20)
    out["debt"] = [F.total_debt({k: (None if pd.isna(r[k]) else r[k]) for k in INSTANTS}) if r[["equity", "assets"]].notna().any()
                   else np.nan for _, r in out.iterrows()]
    out["cash"] = out["cash"].fillna(0) + out["st_investments"].fillna(0)
    out["ebit"] = out["operating_income"].fillna(out["pretax_income"] + out["interest_expense"].fillna(0))
    out["fcf"] = out["cfo"] - out["capex"].abs().fillna(0) - out["sbc"].abs().fillna(0)
    out = out.drop(columns=[c for c in INSTANTS if c not in ("cash", "equity", "assets")]
                   + ["net_interest_income", "noninterest_income"])
    return out.iloc[-QUARTERS:]


def _prices(px: dict, t: str) -> dict:
    """Daily adjusted close for the last 2 years, weekly before that (keeps files small)."""
    if t not in px["Close"]:
        return {}
    # Close is split-adjusted (today's share basis) but not dividend-adjusted: the price people quote.
    s = px["Close"][t].dropna()
    if s.empty:
        return {}
    cut = s.index[-1] - pd.DateOffset(years=2)
    old = s[s.index < cut].resample("W-FRI").last().dropna()
    s = pd.concat([old, s[s.index >= cut]])
    vol = (px["Volume"][t] * px["Close"][t]).dropna()
    d0 = s.index[0]
    return {"d0": f"{d0:%Y-%m-%d}", "dd": [int((d - d0).days) for d in s.index], "p": [_num(v, 5) for v in s.values],
            "close": _num(px["Close"][t].dropna().iloc[-1]),
            "adv": _num(vol.tail(63).median())}


def _trades(trades: pd.DataFrame | None, cik) -> list:
    if trades is None or cik is None or pd.isna(cik):
        return []
    t = trades[trades["cik"] == int(cik)].sort_values("traded").tail(40)
    return [[f"{r.traded:%Y-%m-%d}" if pd.notna(r.traded) else f"{r.filed:%Y-%m-%d}", r.code, _num(r.value, 4),
             _num(r.price, 5), str(r.title or ("Officer" if r.officer else "Director"))[:40]] for r in t.itertuples()]


def company(t: str, cf: dict | None, px: dict, trades=None, cik=None) -> dict:
    from .prices import split_factor_after
    out = {"t": t, "px": _prices(px, t), "ins": _trades(trades, cik)}
    if cf:
        q = quarterly(cf)
        if len(q):
            # Restate share counts onto today's basis so per-share history survives splits.
            sp = px["Stock Splits"][t].dropna() if "Stock Splits" in px and t in px["Stock Splits"] else None
            if sp is not None and (sp > 0).any():
                # Filings may already restate old counts for a split, so keep whichever reading
                # sits closer to today's count.
                last = q["shares"].dropna().iloc[-1] if q["shares"].notna().any() else None
                k = pd.Series([split_factor_after(sp, d) for d in q.index], index=q.index)
                if last:
                    raw, adj = q["shares"], q["shares"] * k
                    q["shares"] = adj.where((np.log(adj / last)).abs() < (np.log(raw / last)).abs(), raw)
            out["q"] = {"d": [f"{d:%Y-%m-%d}" for d in q.index],
                        **{c: [_num(v) for v in q[c].values] for c in q.columns}}
    return out


INDEX_COLS = {  # index.json key -> scored column
    "px": "price", "mc": "mcap", "ir": "implied_return", "cy": "cash_yield", "g": "growth_rate", "pe": "pe",
    "ps": "ps", "pb": "pb", "evebit": "ev_ebit", "fy": "fcf_yield", "ey": "earnings_yield", "rev": "revenue",
    "ni": "net_income", "fcf": "fcf", "nm": "net_margin", "om": "op_margin", "gm": "gross_margin",
    "roic": "roic", "roe": "roe", "rg": "rev_growth", "rg3": "rev_cagr_3y", "nig": "ni_growth",
    "pg3": "profit_ps_cagr_3y", "nd": "net_debt", "nde": "nd_ebitda", "cov": "interest_coverage",
    "r1m": "ret_1m", "rytd": "ret_ytd", "r1y": "ret_1y", "off": "off_high", "ib": "ins_buyers",
    "ibu": "ins_buy_usd", "isu": "ins_sell_usd", "sy": "shareholder_yield", "fs": "fscore",
}


def index_rows(scored: pd.DataFrame, uni: pd.DataFrame) -> list[dict]:
    from . import report, score
    s = scored.copy()
    fin = s["is_financial"].astype(bool)
    passed = s[s["passes"]]
    core = passed[~fin.loc[passed.index]]
    top = set(score.top_n(core, "composite", 25).index)
    rank = {}
    for grp in (core, passed[fin.loc[passed.index]]):
        rank.update({t: i + 1 for i, t in enumerate(grp.sort_values("composite", ascending=False).index)})
    rows = []
    for t, r in s.iterrows():
        row = {"t": t, "n": report._name(r.get("sec_name") or t), "sec": r.get("sector") or "",
               "ind": r.get("industry") or "", "idx": str(uni["index"].get(t, "") or ""),
               "cik": int(uni["cik"][t]) if t in uni.index and pd.notna(uni["cik"][t]) else None,
               "inv": bool(r.get("investable")), "pass": bool(r.get("passes")), "fin": bool(fin[t]),
               "top": t in top, "rank": rank.get(t),
               "failed": [f.strip() for f in str(r.get("failed") or "").split(",") if f.strip()],
               "why": report.why(r) if r.get("passes") else "",
               "flags": [f for f in report._flags(r).split("; ") if f],
               "pend": f"{pd.Timestamp(r['period_end']):%Y-%m-%d}" if pd.notna(r.get("period_end")) else None}
        for k, c in INDEX_COLS.items():
            row[k] = _num(r.get(c), 5)
        rows.append(row)
    return rows


def build(scored: pd.DataFrame, news: dict | None = None, out: Path = OUT) -> Path:
    """Write index.json and per-letter chunks for every company in the scored screen."""
    from . import edgar, insider, prices, universe
    out.mkdir(parents=True, exist_ok=True)
    uni = universe.load().set_index("ticker")
    tickers = list(scored.index)
    px = prices.load(uni.index.tolist())
    trades = insider.build() if insider.TRADES.exists() else None
    chunks: dict[str, dict] = {}
    for i, t in enumerate(tickers):
        cik = uni["cik"].get(t)
        cf = edgar.fetch(int(cik)) if cik is not None and pd.notna(cik) else None
        try:
            chunks.setdefault(_chunk(t), {})[t] = company(t, cf, px, trades, cik)
        except Exception as e:  # one bad filer shouldn't stop the export
            print(f"[app] {t}: {e}", flush=True)
        if (i + 1) % 250 == 0:
            print(f"[app] {i + 1}/{len(tickers)}", flush=True)
    for k, v in chunks.items():
        (out / f"c_{k}.json").write_text(json.dumps(v, separators=(",", ":")))
    d = pd.Timestamp(scored["date"].iloc[0])
    idx = {"asof": f"{d:%Y-%m-%d}", "rows": index_rows(scored, uni), "news": news_rows(news)}
    (out / "index.json").write_text(json.dumps(idx, separators=(",", ":")))
    exp = sorted((out.parent.parent / "reports").glob("explorer_*.html"))
    if exp:  # the backtest/method page rides along with the app
        (out.parent / "explorer.html").write_text(exp[-1].read_text())
    return out


def news_rows(news: dict | None) -> dict:
    return {t: [{"title": h["title"], "src": h["source"], "link": h["link"],
                 "date": f"{h['date']:%b %d}" if pd.notna(h["date"]) else ""} for h in hs]
            for t, hs in (news or {}).items() if hs}


def _chunk(t: str) -> str:
    c = t[0].upper()
    return c if c.isalpha() else "0"

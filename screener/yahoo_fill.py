"""Patch EDGAR gaps with Yahoo's quarterly statements. Live screen only.

Yahoo has ~5 quarters, no filing dates and no restatement history, so it can never feed
the backtest. It is used only where EDGAR is demonstrably worse:
  * debt that exists only in company-specific XBRL tags (we'd otherwise estimate it),
  * filers whose latest quarter SEC's companyfacts API hasn't picked up yet.
Every patched value is flagged in `yahoo_fields` so the report shows its source.
"""
from __future__ import annotations

import io
import json
import time

import pandas as pd

from . import config

CACHE = config.CACHE_DIR / "yahoo"
MAX_AGE_DAYS = 3
LAG_DAYS = 120  # EDGAR TTM older than this is probably missing a filed quarter


def _statements(ticker: str) -> dict[str, pd.DataFrame] | None:
    p = CACHE / f"{ticker}.json"
    if p.exists() and time.time() - p.stat().st_mtime < MAX_AGE_DAYS * 86400:
        raw = json.loads(p.read_text())
    else:
        import yfinance as yf
        try:
            tk = yf.Ticker(ticker)
            frames = {"bs": tk.quarterly_balance_sheet, "is": tk.quarterly_income_stmt,
                      "cf": tk.quarterly_cashflow}
        except Exception as e:
            print(f"[yahoo] {ticker}: {e}")
            return None
        raw = {k: df.to_json(date_format="iso") for k, df in frames.items()}
        CACHE.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(raw))
    out = {}
    for k, js in raw.items():
        df = pd.read_json(io.StringIO(js)) if js else pd.DataFrame()
        if not df.empty:
            df.columns = pd.to_datetime(df.columns).tz_localize(None)
            df = df[sorted(df.columns)]
        out[k] = df
    return out


def _get(df: pd.DataFrame, row: str, col):
    if df.empty or row not in df.index or col not in df.columns:
        return None
    v = df.at[row, col]
    return None if pd.isna(v) else float(v)


def _debt(bs: pd.DataFrame, col) -> float | None:
    """Financial debt excluding leases, matching the EDGAR definition."""
    lt, cur = _get(bs, "Long Term Debt", col), _get(bs, "Current Debt", col)
    if lt is not None or cur is not None:
        return (lt or 0.0) + (cur or 0.0)
    tot = _get(bs, "Total Debt", col)
    if tot is None:
        return None
    return tot - (_get(bs, "Capital Lease Obligations", col) or 0.0)


def _ttm(df: pd.DataFrame, row: str) -> float | None:
    if df.empty or row not in df.index:
        return None
    s = df.loc[row].dropna()
    if len(s) < 4:
        return None
    last4 = s.iloc[-4:]
    if not 230 <= (last4.index[-1] - last4.index[0]).days <= 310:
        return None
    return float(last4.sum())


def patch(v: dict, ticker: str, as_of: pd.Timestamp, debt_estimated: bool) -> list[str]:
    """Update snapshot values `v` in place; returns the names of fields taken from Yahoo."""
    lagged = v.get("period_end") is not None and (as_of - v["period_end"]).days > LAG_DAYS
    if not (debt_estimated or lagged):
        return []
    st = _statements(ticker)
    if not st or st["bs"].empty:
        return []
    bs, inc, cf = st["bs"], st["is"], st["cf"]
    col = bs.columns[-1]
    used = []
    # Only trust a Yahoo balance sheet at least as recent as EDGAR's.
    if v.get("bs_date") is not None and col < v["bs_date"] - pd.Timedelta(days=10):
        return []
    if debt_estimated or (lagged and col > (v.get("bs_date") or col)):
        d = _debt(bs, col)
        if d is not None:
            v.update(lt_debt_total=d, lt_debt_noncurrent=None, lt_debt_current=None,
                     st_debt=None, debt_current=None, total_debt=d, debt_stale=False)
            year_ago = [c for c in bs.columns if abs((col - c).days - 365) <= 45]
            v["total_debt_prev"] = _debt(bs, year_ago[-1]) if year_ago else None
            used.append("debt")
    if lagged and col > v["period_end"]:
        for key, row in (("equity", "Stockholders Equity"), ("assets", "Total Assets"),
                         ("cash", "Cash And Cash Equivalents"),
                         ("st_investments", "Other Short Term Investments"),
                         ("current_assets", "Current Assets"),
                         ("current_liabilities", "Current Liabilities")):
            x = _get(bs, row, col)
            if x is not None:
                v[key] = x
                used.append(key)
        v["bs_date"] = col
        # Levels only: Yahoo lacks the year-earlier TTM, so growth stays on EDGAR.
        flows = (("revenue_ttm", inc, "Total Revenue"), ("net_income_ttm", inc, "Net Income"),
                 ("operating_income_ttm", inc, "Operating Income"),
                 ("cfo_ttm", cf, "Operating Cash Flow"), ("capex_ttm", cf, "Capital Expenditure"))
        got = {k: _ttm(df, r) for k, df, r in flows}
        if got["revenue_ttm"] is not None and got["net_income_ttm"] is not None \
                and not inc.empty and inc.columns[-1] > v["period_end"]:
            for k, x in got.items():
                if x is not None:
                    v[k] = abs(x) if k == "capex_ttm" else x
                    used.append(k)
            v["period_end"] = inc.columns[-1]
    return used

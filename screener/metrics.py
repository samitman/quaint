"""Price + fundamentals -> valuation, quality, trajectory and balance-sheet metrics.

Everything here is a pure function of one company's snapshot values and a price, so the
live screen and the backtest share exactly the same math.
"""
from __future__ import annotations

import math

import pandas as pd

from . import fundamentals as F

TAX_RATE = 0.21
DEBT_RATE_GUESS = 0.055  # statutory; effective rates are too noisy quarter to quarter

# Tickers whose traded class is a fixed fraction of the share unit EDGAR reports.
# BRK reports Class A-equivalent shares; one B share is 1/1500 of an A share. (BRK has
# used custom EPS tags since 2015, so in practice the live screen falls back to Yahoo.)
SHARE_CLASS_RATIO = {"BRK-B": 1500.0}

FINANCIALS = "Financials"

# Earnings swing with a commodity or industry cycle; a low P/E here is often a peak.
CYCLICAL_INDUSTRIES = {
    "Oil & Gas Exploration & Production", "Integrated Oil & Gas", "Oil & Gas Refining & Marketing",
    "Oil & Gas Equipment & Services", "Oil & Gas Drilling", "Coal & Consumable Fuels",
    "Gold", "Silver", "Copper", "Diversified Metals & Mining", "Steel", "Aluminum",
    "Commodity Chemicals", "Fertilizers & Agricultural Chemicals", "Semiconductors",
    "Homebuilding", "Automobile Manufacturers",
    "Construction Machinery & Heavy Transportation Equipment", "Airlines", "Marine Transportation",
    "Forest Products", "Paper & Plastic Packaging Products & Materials",
}
CYCLICAL_TICKERS = {"WDC", "STX", "SNDK"}  # memory/storage inside a non-cyclical GICS industry


def _div(a, b):
    if a is None or b is None or b == 0 or (isinstance(a, float) and math.isnan(a)) \
            or (isinstance(b, float) and math.isnan(b)):
        return None
    return a / b


def _growth(cur, prev):
    """Growth that stays meaningful when the base is negative or tiny."""
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def cagr(annual: dict, years: int = 3):
    s = pd.Series(annual).sort_index()
    if len(s) < years + 1:
        return None
    a, b = s.iloc[-years - 1], s.iloc[-1]
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return (b / a) ** (1 / years) - 1


def ebit(v: dict, suffix: str = "") -> float | None:
    oi = v.get(f"operating_income_ttm{suffix}")
    if oi is not None:
        return oi
    pt = v.get(f"pretax_income_ttm{suffix}")
    if pt is None:
        return None
    return pt + (v.get(f"interest_expense_ttm{suffix}") or 0.0)


def shares(v: dict, ticker: str) -> tuple[float | None, str, pd.Timestamp | None]:
    """Share count on the traded class's basis, which source it came from, and its date."""
    r = SHARE_CLASS_RATIO.get(ticker)
    if r:
        if v.get("eps_implied_shares"):
            return v["eps_implied_shares"] * r, f"eps x {r:g}", v.get("period_end")
        return None, "none", None  # BRK tags EPS with its own extension; see screen.py
    sh = F.shares_outstanding(v)
    if sh is None:
        return None, "none", None
    if sh == v.get("shares_out_dei"):
        return sh, "cover", v.get("shares_out_dei_date")
    if sh == v.get("diluted_shares"):
        return sh, "diluted", v.get("period_end")
    return sh, "eps-implied", v.get("period_end")


def piotroski(v: dict) -> tuple[float | None, int]:
    """Piotroski F-score, rescaled to 0-9 over the signals the filer's tags support.

    Returns (score, number of signals available). Banks lack current assets and gross
    profit, so they get scored on the subset that exists rather than auto-failing.
    """
    tests = []
    assets, assets_p = v.get("assets"), v.get("assets_prev")
    ni, ni_p = v.get("net_income_ttm"), v.get("net_income_ttm_prev")
    cfo = v.get("cfo_ttm")
    roa, roa_p = _div(ni, assets), _div(ni_p, assets_p)
    if roa is not None:
        tests.append(roa > 0)
    if cfo is not None:
        tests.append(cfo > 0)
    if roa is not None and roa_p is not None:
        tests.append(roa > roa_p)
    if cfo is not None and ni is not None and assets:
        tests.append(cfo > ni)
    lev = _div(v.get("lt_debt_noncurrent") if v.get("lt_debt_noncurrent") is not None
               else v.get("lt_debt_total"), assets)
    lev_p = _div(v.get("lt_debt_noncurrent_prev") if v.get("lt_debt_noncurrent_prev") is not None
                 else v.get("lt_debt_total_prev"), assets_p)
    if lev is not None and lev_p is not None:
        tests.append(lev <= lev_p)
    cr = _div(v.get("current_assets"), v.get("current_liabilities"))
    cr_p = _div(v.get("current_assets_prev"), v.get("current_liabilities_prev"))
    if cr is not None and cr_p is not None:
        tests.append(cr > cr_p)
    sh, sh_p = v.get("diluted_shares"), v.get("diluted_shares_prev")
    if sh and sh_p:
        tests.append(sh <= sh_p * 1.01)  # 1% slack for option noise
    rev, rev_p = v.get("revenue_ttm"), v.get("revenue_ttm_prev")
    gm, gm_p = _div(v.get("gross_profit_ttm"), rev), _div(v.get("gross_profit_ttm_prev"), rev_p)
    if gm is not None and gm_p is not None:
        tests.append(gm > gm_p)
    at, at_p = _div(rev, assets), _div(rev_p, assets_p)
    if at is not None and at_p is not None:
        tests.append(at > at_p)
    if len(tests) < 5:
        return None, len(tests)
    return 9.0 * sum(tests) / len(tests), len(tests)


def compute(v: dict, ticker: str, sector: str, price: float | None,
            as_of: pd.Timestamp | None = None, splits: pd.Series | None = None,
            industry: str = "") -> dict:
    """All metrics for one company.

    `price` is Yahoo's split-adjusted Close (today's share basis). `splits` is the
    ticker's split history; shares get restated onto that basis from the date they were
    reported, which keeps historical market caps right across splits.
    """
    m: dict = {"ticker": ticker, "sector": sector, "price": price,
               "period_end": v.get("period_end"), "bs_date": v.get("bs_date")}
    sh, how, when = shares(v, ticker)
    if sh and splits is not None and when is not None:
        from .prices import split_factor_after
        sh *= split_factor_after(splits, when)
    m["shares"], m["shares_source"] = sh, how
    mcap = price * sh if price and sh else None
    m["mcap"] = mcap
    if price and splits is not None and as_of is not None:
        from .prices import split_factor_after
        m["price"] = price * split_factor_after(splits, as_of)  # what it actually traded at

    rev, rev_p = v.get("revenue_ttm"), v.get("revenue_ttm_prev")
    ni, ni_p = v.get("net_income_ttm"), v.get("net_income_ttm_prev")
    cfo, capex = v.get("cfo_ttm"), v.get("capex_ttm") or 0.0
    fcf = cfo - capex if cfo is not None else None
    e, e_p = ebit(v), ebit(v, "_prev")
    dna = v.get("dna_ttm") or 0.0
    debt = v.get("total_debt") or 0.0
    debt_estimated = False
    interest = v.get("interest_expense_ttm")
    if not interest and sector != FINANCIALS and (v.get("net_interest_income_ttm") or 0) < 0:
        interest = -v["net_interest_income_ttm"]  # TXT reports only net interest
    if not _debt_tagged(v) and (interest or 0) > 0:
        # Debt lives only in company-specific tags (AES, TXT). Infer it from interest
        # at a ~5.5% coupon rather than pretend the company is debt-free.
        debt, debt_estimated = interest / DEBT_RATE_GUESS, True
    cash = (v.get("cash") or 0.0) + (v.get("st_investments") or 0.0)
    equity = v.get("equity")
    net_debt = debt - cash
    ev = mcap + net_debt if mcap is not None else None

    m.update(revenue=rev, net_income=ni, fcf=fcf, ebit=e, equity=equity,
             total_debt=debt, cash=cash, net_debt=net_debt, ev=ev,
             debt_stale=v.get("debt_stale", False), debt_tagged=_debt_tagged(v),
             debt_estimated=debt_estimated)
    # Valuation (yields, so negative earnings rank last instead of blowing up).
    m["pe"] = _div(mcap, ni) if ni and ni > 0 else None
    m["earnings_yield"] = _div(ni, mcap)
    m["fcf_yield"] = _div(fcf, mcap)
    m["ev_ebit"] = _div(ev, e) if e and e > 0 and ev and ev > 0 else None
    m["ebit_ev"] = _div(e, ev) if ev and ev > 0 else None
    m["pb"] = _div(mcap, equity) if equity and equity > 0 else None
    m["ps"] = _div(mcap, rev) if rev and rev > 0 else None
    m["net_margin"] = _div(ni, rev) if rev and rev > 0 else None
    m["book_yield"] = _div(equity, mcap)
    # Quality.
    invested = (equity or 0.0) + debt - cash
    m["roic"] = _div(e * (1 - TAX_RATE), invested) if e is not None and invested > 0 else None
    if m["roic"] is None and e is not None and e > 0 and invested <= 0:
        m["roic"] = 1.0  # net cash exceeds book capital: capital-light, cap rather than drop
    m["roe"] = _div(ni, equity) if equity and equity > 0 else None
    m["op_margin"] = _div(e, rev)
    m["op_margin_prev"] = _div(e_p, rev_p)
    m["gross_margin"] = _div(v.get("gross_profit_ttm"), rev)
    m["fcf_conversion"] = _div(fcf, ni) if ni and ni > 0 else None
    # Trajectory.
    m["rev_growth"] = _growth(rev, rev_p)
    m["rev_cagr_3y"] = cagr(v.get("revenue_annual", {}))
    # Per-share growth: acquisitions paid for with new stock don't count as growth.
    sh_now, sh_prev = v.get("diluted_shares"), v.get("diluted_shares_prev")
    ok_sh = sh_now and sh_prev and 0.5 < sh_now / sh_prev < 2.0  # guard against tag scaling
    m["rev_ps_growth"] = (_growth(rev / sh_now, rev_p / sh_prev)
                          if ok_sh and rev is not None and rev_p is not None else m["rev_growth"])
    ra, sa = pd.Series(v.get("revenue_annual", {})), pd.Series(v.get("diluted_shares_annual", {}))
    common = ra.index.intersection(sa.index)
    rps = (ra[common] / sa[common]).sort_index() if len(common) else pd.Series(dtype=float)
    jumps = sa[common].sort_index().pct_change().abs().max() if len(common) > 1 else 0
    m["rev_ps_cagr_3y"] = (cagr(rps.to_dict()) if len(rps) >= 4 and jumps < 1.0
                           else m["rev_cagr_3y"])
    m["cyclical"] = industry in CYCLICAL_INDUSTRIES or ticker in CYCLICAL_TICKERS
    m["ni_growth"] = _growth(ni, ni_p)
    m["margin_change"] = (m["op_margin"] - m["op_margin_prev"]
                          if m["op_margin"] is not None and m["op_margin_prev"] is not None else None)
    roe_p = _div(ni_p, v.get("equity_prev")) if v.get("equity_prev") else None
    m["roe_change"] = m["roe"] - roe_p if m["roe"] is not None and roe_p is not None else None
    # Balance sheet.
    ebitda = e + dna if e is not None else None
    m["ebitda"] = ebitda
    m["nd_ebitda"] = (_div(net_debt, ebitda) if ebitda and ebitda > 0 else
                      (-1.0 if net_debt <= 0 else None))
    m["debt_equity"] = _div(debt, equity) if equity and equity > 0 else None
    m["equity_assets"] = _div(equity, v.get("assets"))
    dp = v.get("total_debt_prev")
    # Debt change, scaled by assets so a $0 -> $10M move doesn't read as +infinity.
    m["debt_change"] = (_div(debt - dp, v.get("assets")) if dp is not None and not debt_estimated
                        and v.get("assets") else None)
    m["debt_prev"] = dp
    m["debt_yoy"] = (debt - dp) / dp if dp and not debt_estimated else (
        0.0 if dp == 0 and debt == 0 else None)
    m["fscore"], m["fscore_n"] = piotroski(v)
    if as_of is not None and m["period_end"] is not None:
        m["data_age_days"] = (pd.Timestamp(as_of) - m["period_end"]).days
    m["is_financial"] = sector == FINANCIALS
    return m


def _debt_tagged(v: dict) -> bool:
    return any(v.get(k) is not None for k in F._DEBT)

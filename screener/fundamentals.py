"""Turn raw XBRL companyfacts into a clean, point-in-time fundamentals snapshot.

The hard parts this handles:
  * Tag drift: companies switch concepts over time (e.g. SalesRevenueNet ->
    RevenueFromContractWithCustomer...). Each metric has an ordered fallback list and
    we merge them, preferring the earlier tag when both cover the same period.
  * Discrete quarters: 10-Qs often report cash flow only year-to-date, and Q4 is never
    reported on its own. We difference same-start cumulative periods to recover each
    3-month quarter, then sum the latest four for TTM.
  * Restatements / look-ahead: only facts filed on or before `as_of` are used, and for a
    given period the most recently filed (as of that date) value wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

# Flow (duration) concepts, in fallback order.
FLOW = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                "SalesRevenueGoodsNet", "RevenuesNetOfInterestExpense"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "income_tax": ["IncomeTaxExpenseBenefit"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
    "net_income": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsForCapitalImprovements"],
    "dna": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
            "DepreciationAmortizationAndAccretionNet", "Depreciation"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
}

# Balance sheet (instant) concepts.
INSTANT = {
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"],
    "st_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                       "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "lt_debt_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "lt_debt_current": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "lt_debt_total": ["LongTermDebt"],
    "st_debt": ["ShortTermBorrowings", "CommercialPaper", "DebtCurrent"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "shares_out_bs": ["CommonStockSharesOutstanding"],
}

# Flows where a negative value would be a sign-convention error, not economics.
_POSITIVE_FLOWS = {"capex", "dna", "diluted_shares", "interest_expense"}


def _facts(cf: dict, concept: str, unit_pref=("USD", "shares", "USD/shares")) -> list[dict]:
    for ns in ("us-gaap", "dei", "ifrs-full"):
        node = cf.get("facts", {}).get(ns, {}).get(concept)
        if node:
            for u in unit_pref:
                if u in node["units"]:
                    return node["units"][u]
    return []


def _frame(cf: dict, concepts: list[str], as_of: date | None, duration: bool) -> pd.DataFrame:
    """Merge fallback concepts into one series of (start, end, val), latest filing wins."""
    rows = []
    for rank, c in enumerate(concepts):
        for f in _facts(cf, c):
            if duration != ("start" in f):
                continue
            rows.append({
                "start": f.get("start"), "end": f["end"], "val": f["val"],
                "filed": f["filed"], "form": f.get("form", ""), "rank": rank,
            })
    if not rows:
        return pd.DataFrame(columns=["start", "end", "val", "filed"])
    df = pd.DataFrame(rows)
    df["end"] = pd.to_datetime(df["end"])
    df["filed"] = pd.to_datetime(df["filed"])
    if duration:
        df["start"] = pd.to_datetime(df["start"])
    if as_of is not None:
        df = df[df["filed"] <= pd.Timestamp(as_of)]
    if df.empty:
        return df
    # Prefer the higher-priority concept, then the newest filing for that exact period.
    keys = ["start", "end"] if duration else ["end"]
    df = df.sort_values(["rank", "filed"], ascending=[True, False])
    df = df.drop_duplicates(keys, keep="first")
    return df.sort_values(keys).reset_index(drop=True)


def _days(df):
    return (df["end"] - df["start"]).dt.days


def discrete_quarters(df: pd.DataFrame) -> pd.Series:
    """Recover 3-month values indexed by quarter end from mixed 3M/6M/9M/12M facts."""
    if df.empty:
        return pd.Series(dtype=float)
    df = df.assign(days=_days(df))
    q: dict[pd.Timestamp, float] = {}
    for _, r in df[df["days"].between(80, 100)].iterrows():
        q[r["end"]] = r["val"]
    # Cumulative periods sharing a start: consecutive differences are quarters.
    for _, g in df[df["days"].between(80, 380)].groupby("start"):
        g = g.sort_values("end")
        prev = None
        for _, r in g.iterrows():
            if prev is not None:
                gap = (r["end"] - prev["end"]).days
                if 80 <= gap <= 100 and r["end"] not in q:
                    q[r["end"]] = r["val"] - prev["val"]
            prev = r
    return pd.Series(q, dtype=float).sort_index()


def annual(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    a = df[_days(df).between(350, 380)]
    return a.set_index("end")["val"].astype(float).sort_index()


def ttm(quarters: pd.Series, annuals: pd.Series, end: pd.Timestamp | None = None) -> float | None:
    """Sum of the four most recent contiguous quarters ending at or before `end`.

    Falls back to the latest fiscal year if quarters are patchy.
    """
    qs = quarters if end is None else quarters[quarters.index <= end]
    if len(qs) >= 4:
        last4 = qs.iloc[-4:]
        span = (last4.index[-1] - last4.index[0]).days
        if 250 <= span <= 300:  # three ~91-day gaps
            return float(last4.sum())
    an = annuals if end is None else annuals[annuals.index <= end]
    if len(an) and (end is None or (end - an.index[-1]).days < 200):
        return float(an.iloc[-1])
    return None


def latest_instant(df: pd.DataFrame) -> tuple[float | None, pd.Timestamp | None]:
    if df.empty:
        return None, None
    r = df.iloc[-1]
    return float(r["val"]), r["end"]


@dataclass
class Snapshot:
    values: dict
    period_end: pd.Timestamp | None


def snapshot(cf: dict, as_of: date | None = None) -> Snapshot:
    """Everything the scoring step needs, using only data public on `as_of`."""
    v: dict = {}
    q_series: dict[str, pd.Series] = {}
    a_series: dict[str, pd.Series] = {}
    for name, concepts in FLOW.items():
        df = _frame(cf, concepts, as_of, duration=True)
        q_series[name] = discrete_quarters(df)
        a_series[name] = annual(df)

    # Anchor every TTM on the latest quarter we have revenue or net income for.
    anchors = [s.index[-1] for s in (q_series["revenue"], q_series["net_income"]) if len(s)]
    anchor = max(anchors) if anchors else None
    v["period_end"] = anchor

    for name in FLOW:
        if name in ("diluted_shares", "eps_diluted"):
            continue
        cur = ttm(q_series[name], a_series[name], anchor)
        prev = ttm(q_series[name], a_series[name], anchor - pd.Timedelta(days=365)) if anchor is not None else None
        if name in _POSITIVE_FLOWS:
            cur = abs(cur) if cur is not None else None
            prev = abs(prev) if prev is not None else None
        v[f"{name}_ttm"] = cur
        v[f"{name}_ttm_prev"] = prev

    sh = q_series["diluted_shares"]
    v["diluted_shares"] = float(sh.iloc[-1]) if len(sh) else (
        float(a_series["diluted_shares"].iloc[-1]) if len(a_series["diluted_shares"]) else None)

    # Revenue history for multi-year growth/consistency.
    v["revenue_annual"] = a_series["revenue"].tail(6).to_dict()
    v["net_income_annual"] = a_series["net_income"].tail(6).to_dict()

    for name, concepts in INSTANT.items():
        val, end = latest_instant(_frame(cf, concepts, as_of, duration=False))
        v[name] = val
        if name == "equity":
            v["bs_date"] = end
    # Year-ago balance sheet for trend metrics (Piotroski needs it).
    for name in ("assets", "current_assets", "current_liabilities", "lt_debt_noncurrent",
                 "lt_debt_total", "equity"):
        df = _frame(cf, INSTANT[name], as_of, duration=False)
        if not df.empty and v.get("bs_date") is not None:
            target = v["bs_date"] - pd.Timedelta(days=365)
            near = df[(df["end"] - target).abs().dt.days <= 45]
            v[f"{name}_prev"] = float(near.iloc[-1]["val"]) if len(near) else None
        else:
            v[f"{name}_prev"] = None

    shares_dei = _frame(cf, ["EntityCommonStockSharesOutstanding"], as_of, duration=False)
    v["shares_out_dei"] = float(shares_dei.iloc[-1]["val"]) if len(shares_dei) else None

    v["total_debt"] = total_debt(v)
    v["total_debt_prev"] = _first(v.get("lt_debt_total_prev"), v.get("lt_debt_noncurrent_prev"))
    return Snapshot(values=v, period_end=anchor)


def _first(*xs):
    for x in xs:
        if x is not None:
            return x
    return None


def total_debt(v: dict) -> float:
    """Financial debt, excluding operating leases. Missing tags count as zero debt
    only when the company reports a balance sheet at all."""
    if v.get("lt_debt_noncurrent") is not None or v.get("lt_debt_current") is not None:
        lt = (v.get("lt_debt_noncurrent") or 0.0) + (v.get("lt_debt_current") or 0.0)
    else:
        lt = v.get("lt_debt_total") or 0.0
    return lt + (v.get("st_debt") or 0.0)


def shares_outstanding(v: dict) -> float | None:
    """Cover-page count is most current, but multi-class issuers (GOOGL, BRK) often
    omit it from companyfacts. Fall back to diluted weighted average when the cover
    number is missing or implausible against it."""
    dei, dil = v.get("shares_out_dei"), v.get("diluted_shares")
    if dei and dil and 0.5 < dei / dil < 1.5:
        return dei
    return _first(dil, dei, v.get("shares_out_bs"))

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

import numpy as np
import pandas as pd

# Flow (duration) concepts, in fallback order.
FLOW = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                "SalesRevenueGoodsNet", "RevenuesNetOfInterestExpense"],
    "gross_profit": ["GrossProfit"],
    # Banks: revenue = net interest income + non-interest income when untagged.
    "net_interest_income": ["InterestIncomeExpenseNet"],
    "noninterest_income": ["NoninterestIncome"],
    "operating_income": ["OperatingIncomeLoss"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"],
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
    "lt_debt_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
                           "ConvertibleLongTermNotesPayable", "ConvertibleDebtNoncurrent",
                           "LongTermNotesPayable", "SeniorLongTermNotes"],
    "lt_debt_current": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
                        "ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent"],
    # Unclassified balance sheets (homebuilders, some insurers) report one debt line.
    "lt_debt_total": ["LongTermDebt", "SeniorNotes", "NotesPayable", "DebtLongtermAndShorttermCombinedAmount"],
    "st_debt": ["ShortTermBorrowings", "CommercialPaper"],
    # Often = current LTD + short-term borrowings, so only used when neither is tagged.
    "debt_current": ["DebtCurrent"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "shares_out_bs": ["CommonStockSharesOutstanding"],
}

# Concepts where filers tag sub-totals with the higher-priority name (e.g. banks put only
# fee income under RevenueFromContractWithCustomer...). For these, the largest value
# reported for a period wins instead of the first concept in the list.
_TAKE_MAX = {"revenue"}

# Per-share and share-count flows are averages, not sums: never difference YTD values.
_AVERAGES = {"diluted_shares", "eps_diluted"}

# Balance-sheet values older than this relative to the latest balance sheet are stale
# (the filer stopped using that tag), not current.
_STALE_DAYS = 45

# Quarter length in days. 13-week quarters are ~91 days, but retailers and PEP run
# 12/12/12/16-week years (84 and 112 days).
Q_MIN, Q_MAX = 75, 120

_DEBT = {"lt_debt_noncurrent", "lt_debt_current", "lt_debt_total", "st_debt", "debt_current"}

_EMPTY = pd.Series(dtype=float, index=pd.DatetimeIndex([]))

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


_COLS = ["start", "end", "val", "filed", "rank"]

# Only periodic reports. Proxies (DEF 14A pay-vs-performance tables) and S-1/8-K
# exhibits re-tag old figures, sometimes mis-scaled (SCHW net income in thousands),
# and as later filings they would otherwise win.
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}


def _frame(cf: dict, concepts: list[str], as_of: date | None, duration: bool,
           take_max: bool = False) -> pd.DataFrame:
    """Merge fallback concepts into one series of (start, end, val), latest filing wins.

    Columns: start/end (NaT start for instants), val, filed, rank (concept priority).
    """
    # The backtest asks for the same concepts at ~100 as-of dates per company, so parse
    # once into numpy arrays (sorted by priority, newest filing first) and cache them.
    cache = cf.setdefault("_parsed", {})
    key = (tuple(concepts), duration)
    if key not in cache:
        cache[key] = _parse(cf, concepts, duration)
    a = cache[key]
    if a is None:
        return pd.DataFrame(columns=_COLS)
    st, en, val, filed, rank = a
    if as_of is not None:
        m = filed <= np.datetime64(pd.Timestamp(as_of).date(), "D")
        st, en, val, filed, rank = st[m], en[m], val[m], filed[m], rank[m]
    if not len(val):
        return pd.DataFrame(columns=_COLS)
    period = st.astype(np.int64) * 100_000 + en.astype(np.int64)
    if take_max:
        # Newest filing per (concept, period), then the largest concept per period.
        _, first = np.unique(period * 100 + rank, return_index=True)
        o = first[np.lexsort((rank[first], -val[first], period[first]))]
    else:
        o = np.arange(len(val))  # already priority-then-newest ordered
    _, keep = np.unique(period[o], return_index=True)  # first per period, sorted by period
    o = o[keep]
    return pd.DataFrame({
        "start": st[o].astype("datetime64[ns]") if duration else pd.NaT,
        "end": en[o].astype("datetime64[ns]"), "val": val[o],
        "filed": filed[o].astype("datetime64[ns]"), "rank": rank[o]})


def _parse(cf: dict, concepts: list[str], duration: bool):
    rows = []
    for rank, c in enumerate(concepts):
        for f in _facts(cf, c):
            if duration != ("start" in f) or f.get("form") not in FORMS:
                continue
            rows.append((f.get("start", "1970-01-01"), f["end"], float(f["val"]), f["filed"], rank))
    if not rows:
        return None
    st, en, val, filed, rank = map(np.array, zip(*rows))
    st, en, filed = (x.astype("datetime64[D]") for x in (st, en, filed))
    o = np.lexsort((-filed.astype(np.int64), rank))
    return st[o], en[o], val[o], filed[o], rank[o]


def _days(df):
    return (df["end"] - df["start"]).dt.days


def discrete_quarters(df: pd.DataFrame) -> pd.Series:
    """Recover 3-month values indexed by quarter end from mixed 3M/6M/9M/12M facts."""
    if df.empty:
        return _EMPTY
    # Plain Python over arrays: this runs ~10^5 times in a backtest and pandas row
    # iteration dominated the profile.
    starts = df["start"].to_numpy()
    ends = df["end"].to_numpy()
    vals = df["val"].to_numpy(dtype=float)
    days = (ends - starts).astype("timedelta64[D]").astype(int)
    q: dict = {}
    for e, d, v in zip(ends, days, vals):
        if Q_MIN <= d <= Q_MAX:
            q[e] = v
    # Cumulative periods sharing a start: consecutive differences are quarters.
    groups: dict = {}
    for st, e, d, v in zip(starts, ends, days, vals):
        if Q_MIN <= d <= 380:
            groups.setdefault(st, []).append((e, v))
    one_day = np.timedelta64(1, "D")
    for g in groups.values():
        g.sort()
        for (e0, v0), (e1, v1) in zip(g, g[1:]):
            gap = (e1 - e0) / one_day
            if Q_MIN <= gap <= Q_MAX and e1 not in q:
                q[e1] = v1 - v0
    if not q:
        return _EMPTY
    return pd.Series(q, dtype=float).sort_index()


def direct_quarters(df: pd.DataFrame) -> pd.Series:
    """Only facts that are themselves ~3 months long (for averages like share counts)."""
    if df.empty:
        return _EMPTY
    d = df[_days(df).between(Q_MIN, Q_MAX)]
    if not len(d):
        return _EMPTY
    d = d.sort_values("filed").drop_duplicates("end", keep="last")
    return d.set_index("end")["val"].astype(float).sort_index()


def annual(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return _EMPTY
    a = df[_days(df).between(350, 380)]
    if not len(a):
        return _EMPTY
    # Same end, different start (fiscal-year change, 52/53-week overlap): newest filing wins.
    a = a.sort_values("filed").drop_duplicates("end", keep="last")
    return a.set_index("end")["val"].astype(float).sort_index()


def ttm(quarters: pd.Series, annuals: pd.Series, end: pd.Timestamp | None = None) -> float | None:
    """Sum of the four most recent contiguous quarters ending at or before `end`.

    Falls back to the latest fiscal year if quarters are patchy.
    """
    qs = quarters if end is None else quarters[quarters.index <= end]
    if end is not None and len(qs) and (end - qs.index[-1]).days > _STALE_DAYS:
        qs = qs.iloc[:0]  # tag went stale before `end`; don't pass off old quarters as TTM
    if len(qs) >= 4:
        last4 = qs.iloc[-4:]
        span = (last4.index[-1] - last4.index[0]).days
        if 230 <= span <= 310:  # three quarter gaps (12-16 weeks each)
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
        df = _frame(cf, concepts, as_of, duration=True, take_max=name in _TAKE_MAX)
        q_series[name] = direct_quarters(df) if name in _AVERAGES else discrete_quarters(df)
        a_series[name] = annual(df)

    # Anchor every TTM on the latest quarter we have revenue or net income for.
    anchors = [s.index[-1] for s in (q_series["revenue"], q_series["net_income"]) if len(s)]
    anchor = max(anchors) if anchors else None
    v["period_end"] = anchor

    for name in FLOW:
        if name in ("diluted_shares", "eps_diluted"):
            continue
        cur = ttm(q_series[name], a_series[name], anchor)
        # 52/53-week years drift a few days, so aim a little past one year back.
        prev = ttm(q_series[name], a_series[name], anchor - pd.Timedelta(days=355)) if anchor is not None else None
        if name in _POSITIVE_FLOWS:
            cur = abs(cur) if cur is not None else None
            prev = abs(prev) if prev is not None else None
        v[f"{name}_ttm"] = cur
        v[f"{name}_ttm_prev"] = prev

    for sfx in ("_ttm", "_ttm_prev"):
        if v[f"revenue{sfx}"] is None and v[f"net_interest_income{sfx}"] is not None:
            v[f"revenue{sfx}"] = v[f"net_interest_income{sfx}"] + (v[f"noninterest_income{sfx}"] or 0.0)

    v["diluted_shares"] = _latest_avg(q_series["diluted_shares"], a_series["diluted_shares"], anchor)
    v["diluted_shares_prev"] = (_latest_avg(q_series["diluted_shares"], a_series["diluted_shares"],
                                            anchor - pd.Timedelta(days=355)) if anchor is not None else None)

    # Shares implied by the latest fiscal year's NI / diluted EPS: a class-mix-proof check
    # on the cover-page share count.
    ni_a, eps_a = a_series["net_income"], a_series["eps_diluted"]
    common = ni_a.index.intersection(eps_a.index)
    v["eps_implied_shares"] = None
    if len(common):
        d = common[-1]
        if abs(eps_a[d]) > 0.05 and ni_a[d] / eps_a[d] > 0:
            v["eps_implied_shares"] = float(ni_a[d] / eps_a[d])

    # Revenue history for multi-year growth/consistency.
    v["revenue_annual"] = a_series["revenue"].tail(6).to_dict()
    v["net_income_annual"] = a_series["net_income"].tail(6).to_dict()

    frames = {n: _frame(cf, c, as_of, duration=False) for n, c in INSTANT.items()}
    # The balance-sheet date is the latest Assets/Equity date; anything older is stale.
    ends = [frames[n]["end"].max() for n in ("assets", "equity") if not frames[n].empty]
    v["bs_date"] = max(ends) if ends else None
    v["debt_stale"] = False
    for name, df in frames.items():
        val, end = latest_instant(df)
        age = (v["bs_date"] - end).days if end is not None and v["bs_date"] is not None else 0
        if name in _DEBT and _STALE_DAYS < age <= 370:
            # Some filers (CAT) only tag debt in the 10-K. Last year-end beats nothing.
            v["debt_stale"] = True
        elif age > _STALE_DAYS:
            val = None
        v[name] = val
    # Year-ago balance sheet for trend metrics (Piotroski needs it).
    for name in ("assets", "current_assets", "current_liabilities", "lt_debt_noncurrent",
                 "lt_debt_total", "equity"):
        df = frames[name]
        if not df.empty and v.get("bs_date") is not None:
            target = v["bs_date"] - pd.Timedelta(days=365)
            near = df[(df["end"] - target).abs().dt.days <= 45]
            v[f"{name}_prev"] = float(near.iloc[-1]["val"]) if len(near) else None
        else:
            v[f"{name}_prev"] = None

    shares_dei = _frame(cf, ["EntityCommonStockSharesOutstanding"], as_of, duration=False)
    if len(shares_dei) and (anchor is None or (shares_dei.iloc[-1]["end"] - anchor).days > -400):
        v["shares_out_dei"] = float(shares_dei.iloc[-1]["val"])
        v["shares_out_dei_date"] = shares_dei.iloc[-1]["end"]
    else:
        v["shares_out_dei"], v["shares_out_dei_date"] = None, None

    v["total_debt"] = total_debt(v)
    v["total_debt_prev"] = _first(v.get("lt_debt_total_prev"), v.get("lt_debt_noncurrent_prev"))
    return Snapshot(values=v, period_end=anchor)


def _latest_avg(q: pd.Series, a: pd.Series, anchor) -> float | None:
    """Most recent quarterly average, else the fiscal-year one, if not stale."""
    for s, tol in ((q, _STALE_DAYS), (a, 200)):
        if len(s) and (anchor is None or (anchor - s.index[-1]).days <= tol):
            return float(s.iloc[-1])
    return None


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
    st = v.get("st_debt")
    if st is None and v.get("lt_debt_current") is None:
        st = v.get("debt_current")
    return lt + (st or 0.0)


def shares_outstanding(v: dict) -> float | None:
    """Pick the share count that at least one other independent source agrees with.

    Sources: cover-page count (most current, but one class only for GOOGL-style
    issuers), diluted weighted average (some filers tag it in millions: MCD reports
    711.1), and FY net income / diluted EPS. Each fails somewhere; two rarely agree
    on a wrong number.
    """
    cands = [(k, v.get(k)) for k in ("shares_out_dei", "diluted_shares", "eps_implied_shares")]
    cands = [(k, x) for k, x in cands if x and x > 0]
    for k, x in cands:  # in preference order
        if any(k2 != k and 0.85 < x / y < 1.18 for k2, y in cands):
            return x
    return _first(v.get("shares_out_dei"), v.get("diluted_shares"), v.get("shares_out_bs"))

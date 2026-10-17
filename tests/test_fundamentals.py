"""Offline tests for the XBRL -> fundamentals math, using hand-built fact payloads."""
from datetime import date

import pandas as pd

from screener import fundamentals as F


def fact(start, end, val, filed, form="10-Q"):
    d = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        d["start"] = start
    return d


def cf(gaap: dict, dei: dict | None = None):
    wrap = lambda m: {k: {"units": {"USD" if not k.endswith("Shares") and "Shares" not in k else "shares": v}}
                      for k, v in m.items()}
    return {"facts": {"us-gaap": wrap(gaap), "dei": wrap(dei or {})}}


# FY = calendar year. Cash flow reported YTD only in 10-Qs (typical), FY in 10-K.
CFO = [
    fact("2023-01-01", "2023-03-31", 10, "2023-05-01"),
    fact("2023-01-01", "2023-06-30", 25, "2023-08-01"),
    fact("2023-01-01", "2023-09-30", 45, "2023-11-01"),
    fact("2023-01-01", "2023-12-31", 70, "2024-02-15", "10-K"),
    fact("2024-01-01", "2024-03-31", 12, "2024-05-01"),
    fact("2024-01-01", "2024-06-30", 30, "2024-08-01"),
]


def test_discrete_quarters_from_ytd():
    df = F._frame(cf({"NetCashProvidedByUsedInOperatingActivities": CFO}),
                  F.FLOW["cfo"], None, duration=True)
    q = F.discrete_quarters(df)
    assert q.tolist() == [10, 15, 20, 25, 12, 18]


def test_ttm_rolls_forward():
    df = F._frame(cf({"NetCashProvidedByUsedInOperatingActivities": CFO}),
                  F.FLOW["cfo"], None, duration=True)
    q, a = F.discrete_quarters(df), F.annual(df)
    assert F.ttm(q, a) == 20 + 25 + 12 + 18
    assert F.ttm(q, a, pd.Timestamp("2023-12-31")) == 70


def test_point_in_time_hides_future_filings():
    df = F._frame(cf({"NetCashProvidedByUsedInOperatingActivities": CFO}),
                  F.FLOW["cfo"], date(2024, 3, 1), duration=True)
    q, a = F.discrete_quarters(df), F.annual(df)
    assert q.index[-1] == pd.Timestamp("2023-12-31")
    assert F.ttm(q, a) == 70


def test_restatement_latest_filing_wins_but_not_before_it_was_filed():
    facts = [fact("2023-01-01", "2023-12-31", 100, "2024-02-15", "10-K"),
             fact("2023-01-01", "2023-12-31", 90, "2025-02-15", "10-K")]
    c = cf({"Revenues": facts})
    assert F.annual(F._frame(c, F.FLOW["revenue"], None, True)).iloc[-1] == 90
    assert F.annual(F._frame(c, F.FLOW["revenue"], date(2024, 6, 1), True)).iloc[-1] == 100


def test_concept_fallback_merges_tag_switch():
    old = [fact("2017-01-01", "2017-12-31", 50, "2018-02-15", "10-K")]
    new = [fact("2018-01-01", "2018-12-31", 60, "2019-02-15", "10-K")]
    c = cf({"SalesRevenueNet": old,
            "RevenueFromContractWithCustomerExcludingAssessedTax": new})
    a = F.annual(F._frame(c, F.FLOW["revenue"], None, True))
    assert a.tolist() == [50, 60]


def test_total_debt_and_share_fallback():
    v = {"lt_debt_noncurrent": 100.0, "lt_debt_current": 20.0, "lt_debt_total": 999.0, "st_debt": 5.0}
    assert F.total_debt(v) == 125.0
    assert F.total_debt({"lt_debt_total": 50.0}) == 50.0
    assert F.shares_outstanding({"shares_out_dei": 10.0, "diluted_shares": 10.5}) == 10.0
    # Multi-class issuer reporting one class on the cover: implausible, use diluted.
    assert F.shares_outstanding({"shares_out_dei": 3.0, "diluted_shares": 12.0}) == 12.0

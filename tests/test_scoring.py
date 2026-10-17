"""Offline tests for metrics, filters and sector-relative scoring."""
import numpy as np
import pandas as pd

from screener import metrics as M, prices as P, score as S


def test_split_factor_restates_old_share_counts():
    splits = pd.Series([0, 4.0, 0, 10.0], index=pd.to_datetime(
        ["2020-01-01", "2020-08-31", "2021-01-01", "2024-06-10"]))
    assert P.split_factor_after(splits, "2020-06-30") == 40.0
    assert P.split_factor_after(splits, "2022-01-01") == 10.0
    assert P.split_factor_after(splits, "2025-01-01") == 1.0


def test_market_cap_across_split_uses_traded_price():
    # 100 shares reported before a 10:1 split; Yahoo's adjusted close is 1/10 of traded.
    v = {"shares_out_dei": 100.0, "shares_out_dei_date": pd.Timestamp("2024-03-31"),
         "diluted_shares": 100.0, "period_end": pd.Timestamp("2024-03-31")}
    splits = pd.Series([10.0], index=pd.to_datetime(["2024-06-10"]))
    m = M.compute(v, "X", "Tech", price=5.0, as_of=pd.Timestamp("2024-05-01"), splits=splits)
    assert m["mcap"] == 5.0 * 1000          # adjusted price x restated shares
    assert m["price"] == 50.0                # what it traded at on the day


def test_eps_implied_shares_override_single_class_cover_count():
    # Cover page shows one class; diluted and EPS-implied agree on the total.
    v = {"shares_out_dei": 3.0, "diluted_shares": 12.2, "eps_implied_shares": 12.0}
    assert M.shares(v, "X")[:2] == (12.2, "diluted")
    # Diluted mis-tagged in millions (MCD): cover and EPS-implied agree.
    v = {"shares_out_dei": 707e6, "diluted_shares": 711.1, "eps_implied_shares": 716e6}
    assert M.shares(v, "X")[:2] == (707e6, "cover")


def _xs(n=20):
    rng = np.random.default_rng(0)
    df = pd.DataFrame({
        "sector": ["A"] * n + ["B"] * n, "is_financial": False,
        "mcap": 5e9, "price": 20.0, "adv_usd": 50e6, "data_age_days": 60,
        "net_income": 1.0, "fcf": 1.0, "nd_ebitda": 0.5, "fscore": 7.0,
    }, index=[f"T{i}" for i in range(2 * n)])
    cols = {c.lstrip("-") for a, b in S.BUCKETS.values() for c in a + b}
    for c in cols - {"nd_ebitda"}:
        df[c] = rng.normal(size=2 * n)
    # Sector B is uniformly 10x "cheaper": sector-relative scores must not care.
    value = S.BUCKETS["value"][0]
    df.loc[df["sector"] == "B", value] *= 10
    return df


def test_scores_are_sector_relative():
    s = S.score(_xs())
    a, b = s[s.sector == "A"]["value"], s[s.sector == "B"]["value"]
    assert abs(a.mean() - b.mean()) < 0.05
    assert s["composite"].between(0, 1).all()


def test_filters_fail_closed_on_missing_data():
    df = _xs()
    df.loc["T0", "nd_ebitda"] = np.nan
    df.loc["T1", "fcf"] = -1
    df.loc["T2", "is_financial"] = True
    df.loc["T2", ["fcf", "nd_ebitda"]] = [-1, 9.0]  # exempt for financials
    s = S.score(df)
    assert not s.loc["T0", "passes"] and "low_leverage" in s.loc["T0", "failed"]
    assert not s.loc["T1", "passes"]
    assert s.loc["T2", "passes"]


def test_bargain_rewards_cheap_given_quality():
    # Market prices quality: value score falls as profitability rises. One name breaks
    # the pattern (high quality AND cheap): it must top the bargain ranking, even though
    # the worst-quality name is cheaper in absolute terms.
    n = 40
    q = np.linspace(0, 1, n)
    df = pd.DataFrame({"sector": "A", "is_financial": False, "mcap": 5e9, "price": 20.0,
                       "adv_usd": 50e6, "data_age_days": 60, "net_income": 1.0, "fcf": 1.0,
                       "nd_ebitda": 0.5, "fscore": 7.0}, index=[f"T{i}" for i in range(n)])
    df["roic"] = df["op_margin"] = df["fcf_conversion"] = q
    df["earnings_yield"] = df["fcf_yield"] = df["ebit_ev"] = df["book_yield"] = 1 - q
    df.loc["T38", ["earnings_yield", "fcf_yield", "ebit_ev", "book_yield"]] = 0.9
    s = S.score(df)
    assert s["bargain"].idxmax() == "T38"
    assert s["value"].idxmax() == "T0"

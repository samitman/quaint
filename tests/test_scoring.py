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
        "mcap": 2e9, "price": 20.0, "adv_usd": 50e6, "data_age_days": 60, "ps": 1.0, "net_margin": 0.1,
        "net_income": 1.0, "fcf": 1.0, "nd_ebitda": 0.5, "fscore": 7.0,
    }, index=[f"T{i}" for i in range(2 * n)])
    gates = {"rev_growth": 0.1, "rev_cagr_3y": 0.1, "rev_ps_growth": 0.1, "rev_ps_cagr_3y": 0.1,
             "roic": 0.2, "roe": 0.2, "margin_change": 0.0, "roe_change": 0.0,
             "debt_yoy": 0.0, "debt_change": 0.0, "op_margin": 0.2, "op_margin_prev": 0.15}
    cols = {c.lstrip("-") for a, b in S.BUCKETS.values() for c in a + b}
    for c in cols - {"nd_ebitda"}:
        df[c] = rng.normal(size=2 * n)
    # Sector B is uniformly 10x "cheaper": sector-relative scores must not care.
    for c, x in gates.items():
        df[c] = x
    df["ps"] = np.linspace(0.5, 1.5, 2 * n)  # T0-T9 cheaper than their sector's median
    value = S.BUCKETS["value"][0]
    df.loc[df["sector"] == "B", value] *= 10
    return df


def test_scores_are_sector_relative():
    s = S.score(_xs())
    a, b = s[s.sector == "A"]["value"], s[s.sector == "B"]["value"]
    assert abs(a.mean() - b.mean()) < 0.05
    assert s[["bargain", "blend"]].stack().between(0, 1).all()


def test_filters_fail_closed_on_missing_data():
    df = _xs()
    df.loc["T0", "nd_ebitda"] = np.nan
    df.loc["T1", "fcf"] = -1
    df.loc["T2", "is_financial"] = True
    df.loc["T2", ["fcf", "nd_ebitda"]] = [-1, 9.0]  # exempt for financials
    df.loc["T3", "rev_ps_cagr_3y"] = -0.01  # growing this year, shrinking over 3: fails
    s = S.score(df)
    assert not s.loc["T3", "passes"] and s.loc["T3", "failed"] == "revenue_growing"
    assert not s.loc["T0", "passes"] and "low_leverage" in s.loc["T0", "failed"]
    assert not s.loc["T1", "passes"]
    assert s.loc["T2", "passes"]


def test_bargain_rewards_cheap_given_quality():
    # Market prices quality: value score falls as profitability rises. One name breaks
    # the pattern (high quality AND cheap): it must top the bargain ranking, even though
    # the worst-quality name is cheaper in absolute terms.
    n = 40
    q = np.linspace(0, 1, n)
    df = pd.DataFrame({"sector": "A", "is_financial": False, "mcap": 2e9, "price": 20.0, "ps": 1.0, "net_margin": 0.1,
                       "adv_usd": 50e6, "data_age_days": 60, "net_income": 1.0, "fcf": 1.0,
                       "nd_ebitda": 0.5, "fscore": 7.0}, index=[f"T{i}" for i in range(n)])
    df["roic"] = df["op_margin"] = df["fcf_conversion"] = q
    for c in ("rev_growth", "rev_cagr_3y", "rev_ps_growth", "rev_ps_cagr_3y", "margin_change",
              "debt_yoy", "debt_change"):
        df[c] = 0.05
    df["op_margin_prev"] = q / 1.1
    df["earnings_yield"] = df["fcf_yield"] = df["ebit_ev"] = df["book_yield"] = 1 - q
    df.loc["T38", ["earnings_yield", "fcf_yield", "ebit_ev", "book_yield"]] = 0.9
    s = S.score(df)
    assert s["bargain"].idxmax() == "T38"
    assert s["value"].idxmax() == "T0"


def test_implied_return_is_yield_plus_capped_growth():
    # Lower of TTM and 3y per-share growth, capped; financials use earnings yield only.
    df = pd.DataFrame({"is_financial": [False, False, True, False],
                       "earnings_yield": [0.06, 0.06, 0.10, 0.06], "fcf_yield": [0.08, 0.08, -0.5, 0.08],
                       "rev_ps_growth": [0.10, 2.50, 0.04, 0.62], "rev_ps_cagr_3y": [0.12, 1.00, np.nan, 0.01]})
    r = S.implied_return(df)
    assert np.allclose(r, [0.07 + 0.10, 0.07 + S.MAX_GROWTH, 0.10 + 0.04, 0.07 + 0.01])

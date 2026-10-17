"""Hard filters (step 2) and sector-relative composite scoring (step 3).

Shared by the live screen and the backtest: both pass a metrics DataFrame (one row per
ticker, columns from metrics.compute plus price/adv) and get the same answer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# --- Step 2: hard filters --------------------------------------------------------------
# Sam's small-cap value profile (2026-10-17). All overridable from the CLI.
MIN_MCAP = 300e6
MAX_MCAP = 5e9
MIN_PRICE = 5.0
MIN_ADV = 2e6           # median daily dollar volume; easy to trade at personal size
MAX_PS = 4.0            # hard ceiling on price / sales
PS_VS_SECTOR = True     # and below the median P/S of investable sector peers
MIN_NET_MARGIN = 0.05   # net income / revenue
US_ONLY = True          # US business address per SEC
MAX_ND_EBITDA = 1.0
MIN_FSCORE = 6.0
MAX_DATA_AGE_DAYS = 200
MAX_MARGIN_DROP = 0.02   # operating margin (ROE for financials) may slip at most 2pp y/y
MAX_DEBT_GROWTH = 0.25   # debt may grow at most 25% y/y unless near net cash
MIN_RETURN_ON_CAPITAL = 0.10  # ROIC (ROE for financials)
# Mortgage REITs run 6-10x leverage on rate bets and book mark-to-market swings as
# earnings, so "P/E 5, 0.8x book" is their permanent state, not a bargain signal.
EXCLUDED_INDUSTRIES = {"Mortgage REITs"}  # TTM must end within ~2 quarters of the screen date


def filters(df: pd.DataFrame) -> pd.DataFrame:
    """Boolean frame, one column per rule. A NaN metric fails its rule: if we can't
    verify it, we don't buy it. Financials skip FCF and ND/EBITDA (meaningless for
    balance-sheet lenders) and are held to a profitability bar instead."""
    fin = df["is_financial"].astype(bool)
    f = pd.DataFrame(index=df.index)
    f["mcap"] = df["mcap"].between(MIN_MCAP, MAX_MCAP)
    f["price"] = df["price"] > MIN_PRICE
    f["liquidity"] = df["adv_usd"] > MIN_ADV
    f["fresh_data"] = df["data_age_days"] <= MAX_DATA_AGE_DAYS
    f["profitable"] = df["net_income"] > 0
    f["fcf_positive"] = fin | (df["fcf"] > 0)
    f["low_leverage"] = fin | (df["nd_ebitda"] < MAX_ND_EBITDA)
    f["fscore"] = df["fscore"] >= MIN_FSCORE
    # P/S = P/E x net margin, so an absolute cutoff mostly picks industries; compare to
    # sector peers instead, with a hard ceiling.
    f["price_to_sales"] = df["ps"] < MAX_PS
    if PS_VS_SECTOR:
        peers = f[["mcap", "price", "liquidity", "fresh_data"]].all(axis=1)
        med = df["ps"].where(peers).groupby(df["sector"]).transform("median")
        f["price_to_sales"] &= df["ps"] < med
    f["net_margin"] = df["net_margin"] >= MIN_NET_MARGIN
    if US_ONLY and "us_hq" in df:
        f["us_company"] = df["us_hq"].astype(bool)
    # Growth gates: a bargain must be a growing, improving business, not just a cheap one.
    # Three-year growth guards against one-year cyclical spikes (memory, commodities).
    # Per share, so growth bought by issuing stock doesn't count.
    f["revenue_growing"] = (df["rev_ps_growth"] > 0) & ((df["rev_ps_cagr_3y"] > 0)
                                                        | (fin & df["rev_ps_cagr_3y"].isna()))
    # A strong business earns well above its cost of capital.
    f["quality"] = df["roic"].where(~fin, df.get("roe")) >= MIN_RETURN_ON_CAPITAL
    margin_trend = df["margin_change"].where(~fin, df.get("roe_change"))
    f["margins_holding"] = margin_trend > -MAX_MARGIN_DROP
    # Debt discipline: borrowing may rise only if the balance sheet is near net cash or
    # the increase is small relative to assets.
    f["debt_discipline"] = fin | (df["nd_ebitda"] < 0.5) | (df["debt_yoy"] <= MAX_DEBT_GROWTH) \
        | (df["debt_change"] <= 0.02)
    if "industry" in df:
        f["business_model"] = ~df["industry"].isin(EXCLUDED_INDUSTRIES)
    return f.fillna(False)


def investable(df: pd.DataFrame) -> pd.Series:
    """Size/liquidity/data rules only: the benchmark universe for the backtest."""
    f = filters(df)
    return f[["mcap", "price", "liquidity", "fresh_data"]].all(axis=1)


# --- Step 3: sector-relative scores ----------------------------------------------------
# Each bucket averages sector percentiles of its inputs; a leading "-" means lower is
# better. Financials get their own lists: no EV, no FCF, no EBITDA.
BUCKETS = {
    "value": (["earnings_yield", "fcf_yield", "ebit_ev", "book_yield"],
              ["earnings_yield", "book_yield"]),
    "profitability": (["roic", "op_margin", "fcf_conversion"], ["roe"]),
    "balance_sheet": (["-nd_ebitda", "-debt_change", "-debt_equity"], ["equity_assets", "-debt_change"]),
    "growth": (["rev_growth", "rev_cagr_3y", "margin_change", "ni_growth"],
               ["rev_growth", "rev_cagr_3y", "ni_growth", "roe_change"]),
    # 100 = most beaten down. Displayed, not ranked on (see DEFAULT_WEIGHTS).
    "price_action": (["-ret_1y", "-off_high"], ["-ret_1y", "-off_high"]),
}
FUNDAMENTALS = ["profitability", "balance_sheet", "growth"]

# Blend weights. Price is zero: a big drop is a reason to look, not evidence of value,
# and 6-12 month losers tend to keep losing. The backtest decides if that changes.
DEFAULT_WEIGHTS = {"value": 0.40, "growth": 0.25, "profitability": 0.20,
                   "balance_sheet": 0.15, "price_action": 0.0}
RANK_BY = "implied_return"  # or "bargain", "blend"
MAX_GROWTH = 0.20  # no business compounds revenue at 40% forever; cap what we pay for
MIN_FIT = 15  # names needed to fit a sector's valuation model; else pooled fit


def _sector_pct(df: pd.DataFrame, col: str, peers: pd.Series) -> pd.Series:
    """Percentile within sector, computed against `peers` (the investable universe), so a
    stock is compared with every sector name of size, not just filter survivors."""
    sign = -1.0 if col.startswith("-") else 1.0
    col = col.lstrip("-")
    out = pd.Series(np.nan, index=df.index)
    if col not in df:
        return out
    x = sign * df[col].astype(float)
    for sec, idx in df.groupby("sector").groups.items():
        ref = x[idx][peers[idx]].dropna().sort_values().to_numpy()
        if len(ref) < 5:
            continue
        vals = x[idx].dropna()
        # Fraction of peers this value beats (ties get half), i.e. a mid-rank percentile.
        lo = np.searchsorted(ref, vals, side="left")
        hi = np.searchsorted(ref, vals, side="right")
        out[vals.index] = (lo + hi) / 2 / len(ref)
    return out


def _bucket(df, cols, cols_fin, peers):
    fin = df["is_financial"].astype(bool)
    pct = {c: _sector_pct(df, c, peers) for c in set(cols) | set(cols_fin)}
    a = pd.concat([pct[c] for c in cols], axis=1).mean(axis=1, skipna=True)
    b = pd.concat([pct[c] for c in cols_fin], axis=1).mean(axis=1, skipna=True)
    return a.where(~fin, b)


def _pct_within(x: pd.Series, groups: pd.Series, peers: pd.Series) -> pd.Series:
    out = pd.Series(np.nan, index=x.index)
    for _, idx in groups.groupby(groups).groups.items():
        ref = x[idx][peers[idx]].dropna().sort_values().to_numpy()
        vals = x[idx].dropna()
        if len(ref) < 5 or vals.empty:
            continue
        out[vals.index] = (np.searchsorted(ref, vals, "left") + np.searchsorted(ref, vals, "right")) / 2 / len(ref)
    return out


def bargain(df: pd.DataFrame, peers: pd.Series) -> pd.Series:
    """Cheapness the fundamentals don't explain.

    Within each sector, regress the value score on profitability, balance-sheet and
    growth scores across investable peers: the fit is the valuation the market
    typically grants that quality. Residual > 0 means cheaper than its quality would
    predict. Returned as a sector percentile of the residual (1 = biggest bargain).
    """
    X = df[FUNDAMENTALS].fillna(0.5)  # unknown quality is treated as average
    y = df["value"]
    resid = pd.Series(np.nan, index=df.index)

    def fit(mask):
        m = mask & y.notna()
        A = np.column_stack([np.ones(m.sum()), X[m].to_numpy()])
        coef, *_ = np.linalg.lstsq(A, y[m].to_numpy(), rcond=None)
        return coef

    pooled = fit(peers)
    for _, idx in df.groupby("sector").groups.items():
        sec = df.index.isin(idx)
        coef = fit(pd.Series(sec, index=df.index) & peers) if (peers & sec & y.notna()).sum() >= MIN_FIT else pooled
        rows = sec & y.notna().to_numpy()
        resid[rows] = y[rows] - np.column_stack([np.ones(rows.sum()), X[rows].to_numpy()]) @ coef
    return _pct_within(resid, df["sector"], peers)


def growth_rate(df: pd.DataFrame) -> pd.Series:
    """Revenue-per-share growth we're willing to pay for: the LOWER of TTM and 3-year
    CAGR (persistence, not a one-year spike), clipped to [0, MAX_GROWTH]."""
    g = pd.concat([df["rev_ps_growth"], df["rev_ps_cagr_3y"]], axis=1).min(axis=1)
    return g.clip(0, MAX_GROWTH)


def cash_yield(df: pd.DataFrame) -> pd.Series:
    """Mean of earnings and FCF yield; earnings yield alone for financials."""
    fin = df["is_financial"].astype(bool)
    return pd.concat([df["earnings_yield"], df["fcf_yield"].where(~fin)], axis=1).mean(axis=1)


def implied_return(df: pd.DataFrame) -> pd.Series:
    """Cash yield + growth: what an owner earns per year if the multiple never changes.
    Absolute, so it compares across non-financial sectors. Financials are ranked
    separately because their earnings yield is levered."""
    return cash_yield(df) + growth_rate(df)


def score(df: pd.DataFrame, weights: dict | None = None, rank_by: str | None = None) -> pd.DataFrame:
    """Adds bucket scores, blend, bargain and the ranking `composite` (all 0-1, higher =
    better buy), plus filter results."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    peers = investable(df)
    out = df.copy()
    for b, (cols, cols_fin) in BUCKETS.items():
        out[b] = _bucket(df, cols, cols_fin, peers)
    tot = sum(w.values())
    out["blend"] = sum(w[b] * out[b].fillna(0.5) for b in BUCKETS) / tot
    out["bargain"] = bargain(out, peers)
    out["cash_yield"] = cash_yield(df)
    out["growth_rate"] = growth_rate(df)
    out["implied_return"] = out["cash_yield"] + out["growth_rate"]
    out["composite"] = out[rank_by or RANK_BY]
    f = filters(df)
    out["investable"] = peers
    out["passes"] = f.all(axis=1)
    out["failed"] = f.apply(lambda r: ",".join(c for c, ok in r.items() if not ok), axis=1)
    return out


MAX_PER_SECTOR = 7  # ~30% of a 25-name list


def top_n(df: pd.DataFrame, col: str, n: int, per_sector: int = MAX_PER_SECTOR) -> pd.DataFrame:
    """Best `n` by `col`, at most `per_sector` from any one sector (diversification)."""
    ranked = df.sort_values(col, ascending=False)
    ranked = ranked[ranked[col].notna()]
    keep = ranked.groupby("sector").cumcount() < per_sector
    return ranked[keep].head(n)

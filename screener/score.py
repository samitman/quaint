"""Hard filters (step 2) and sector-relative composite scoring (step 3).

Shared by the live screen and the backtest: both pass a metrics DataFrame (one row per
ticker, columns from metrics.compute plus price/adv) and get the same answer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# --- Step 2: hard filters --------------------------------------------------------------
MIN_MCAP = 2e9
MIN_PRICE = 5.0
MIN_ADV = 10e6
MAX_ND_EBITDA = 1.5
MIN_FSCORE = 6.0
MAX_DATA_AGE_DAYS = 200
# Mortgage REITs run 6-10x leverage on rate bets and book mark-to-market swings as
# earnings, so "P/E 5, 0.8x book" is their permanent state, not a bargain signal.
EXCLUDED_INDUSTRIES = {"Mortgage REITs"}  # TTM must end within ~2 quarters of the screen date


def filters(df: pd.DataFrame) -> pd.DataFrame:
    """Boolean frame, one column per rule. A NaN metric fails its rule: if we can't
    verify it, we don't buy it. Financials skip FCF and ND/EBITDA (meaningless for
    balance-sheet lenders) and are held to a profitability bar instead."""
    fin = df["is_financial"].astype(bool)
    f = pd.DataFrame(index=df.index)
    f["mcap"] = df["mcap"] > MIN_MCAP
    f["price"] = df["price"] > MIN_PRICE
    f["liquidity"] = df["adv_usd"] > MIN_ADV
    f["fresh_data"] = df["data_age_days"] <= MAX_DATA_AGE_DAYS
    f["profitable"] = df["net_income"] > 0
    f["fcf_positive"] = fin | (df["fcf"] > 0)
    f["low_leverage"] = fin | (df["nd_ebitda"] < MAX_ND_EBITDA)
    f["fscore"] = df["fscore"] >= MIN_FSCORE
    if "industry" in df:
        f["business_model"] = ~df["industry"].isin(EXCLUDED_INDUSTRIES)
    return f.fillna(False)


def investable(df: pd.DataFrame) -> pd.Series:
    """Size/liquidity/data rules only: the benchmark universe for the backtest."""
    f = filters(df)
    return f[["mcap", "price", "liquidity", "fresh_data"]].all(axis=1)


# --- Step 3: sector-relative scores ----------------------------------------------------
# Higher is better for every input. Financials get their own lists: no EV, no FCF.
VALUE = ["earnings_yield", "fcf_yield", "ebit_ev", "book_yield"]
VALUE_FIN = ["earnings_yield", "book_yield"]
QUALITY = ["roic", "op_margin", "fcf_conversion"]
QUALITY_FIN = ["roe"]
TRAJ = ["rev_growth", "rev_cagr_3y", "margin_change", "ni_growth"]
TRAJ_FIN = ["rev_growth", "rev_cagr_3y", "ni_growth", "roe_change"]

DEFAULT_WEIGHTS = {"value": 0.5, "quality": 0.25, "trajectory": 0.25}


def _sector_pct(df: pd.DataFrame, col: str, peers: pd.Series) -> pd.Series:
    """Percentile within sector, computed among `peers` (the investable universe), so a
    stock is compared with every sector name of size, not just filter survivors."""
    x = df[col].astype(float).clip(*df.loc[peers, col].astype(float).quantile([0.01, 0.99]))
    out = pd.Series(np.nan, index=df.index)
    for sec, idx in df.groupby("sector").groups.items():
        ref = x[idx][peers[idx]].dropna().sort_values().to_numpy()
        if len(ref) < 5:
            continue
        vals = x[idx]
        ok = vals.notna()
        # Fraction of peers this value beats (ties get half), i.e. a mid-rank percentile.
        lo = np.searchsorted(ref, vals[ok], side="left")
        hi = np.searchsorted(ref, vals[ok], side="right")
        out[vals[ok].index] = (lo + hi) / 2 / len(ref)
    return out


def _bucket(df, cols, cols_fin, peers):
    fin = df["is_financial"].astype(bool)
    pct = {c: _sector_pct(df, c, peers) for c in set(cols) | set(cols_fin)}
    a = pd.concat([pct[c] for c in cols], axis=1).mean(axis=1, skipna=True)
    b = pd.concat([pct[c] for c in cols_fin], axis=1).mean(axis=1, skipna=True)
    return a.where(~fin, b)


def score(df: pd.DataFrame, weights: dict | None = None) -> pd.DataFrame:
    """Adds value/quality/trajectory/composite (0-1, higher = bigger bargain) and pass."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    peers = investable(df)
    out = df.copy()
    out["value"] = _bucket(df, VALUE, VALUE_FIN, peers)
    out["quality"] = _bucket(df, QUALITY, QUALITY_FIN, peers)
    out["trajectory"] = _bucket(df, TRAJ, TRAJ_FIN, peers)
    tot = sum(w.values())
    out["composite"] = (w["value"] * out["value"].fillna(0) + w["quality"] * out["quality"].fillna(0)
                        + w["trajectory"] * out["trajectory"].fillna(0)) / tot
    f = filters(df)
    out["investable"] = peers
    out["passes"] = f.all(axis=1)
    out["failed"] = f.apply(lambda r: ",".join(c for c, ok in r.items() if not ok), axis=1)
    return out

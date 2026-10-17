"""Insider trading from SEC Form 4 (quarterly "Insider Transactions Data Sets").

Only open-market trades count: code P (purchase) and S (sale), by officers and directors.
Grants, option exercises, gifts and 10%-owner funds are noise for this purpose. Sales under a
10b5-1 plan (flagged since 2023) are pre-scheduled, so they are dropped too. Everything is
dated by FILING_DATE, which is when the market could first see it (point-in-time safe).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, http

DIR = config.CACHE_DIR / "insider"
TRADES = DIR / "trades.parquet"
INDEX = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
FIRST_YEAR = 2016
LOOKBACK_DAYS = 180
MIN_TRADE_USD = 10_000  # ignore token trades


def _headers() -> dict:
    return {"User-Agent": config.require_sec_agent()}


def download() -> list[Path]:
    """Fetch every quarterly zip since FIRST_YEAR that isn't cached. Past quarters never change;
    the newest one is re-fetched when the SEC republishes it (size differs)."""
    DIR.mkdir(parents=True, exist_ok=True)
    page = http.get(INDEX, headers=_headers()).text
    urls = sorted(set(re.findall(r'href="([^"]*?(\d{4})q\d_form345\.zip)"', page)))
    got = []
    for url, year in urls:
        if int(year) < FIRST_YEAR:
            continue
        f = DIR / Path(url).name
        if not f.exists():
            r = http.get("https://www.sec.gov" + url, headers=_headers(), timeout=120)
            r.raise_for_status()
            f.write_bytes(r.content)
            print(f"[insider] {f.name}", flush=True)
        got.append(f)
    return got


def _read(z: zipfile.ZipFile, name: str, cols: list[str]) -> pd.DataFrame:
    with z.open(name) as fh:
        head = fh.readline().decode().rstrip("\n").split("\t")
    use = [c for c in cols if c in head]
    with z.open(name) as fh:
        df = pd.read_csv(fh, sep="\t", usecols=use, dtype=str, quoting=3, on_bad_lines="skip")
    for c in set(cols) - set(use):
        df[c] = None
    return df


def _parse(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        sub = _read(z, "SUBMISSION.tsv", ["ACCESSION_NUMBER", "FILING_DATE", "ISSUERCIK",
                                           "DOCUMENT_TYPE", "AFF10B5ONE"])
        tr = _read(z, "NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE",
                                              "TRANS_SHARES", "TRANS_PRICEPERSHARE",
                                              "TRANS_ACQUIRED_DISP_CD"])
        own = _read(z, "REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERCIK",
                                               "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"])
    tr = tr[tr["TRANS_CODE"].isin(["P", "S"])]
    sub = sub[sub["DOCUMENT_TYPE"].isin(["4", "4/A"])]
    # A filing can list several owners (joint filers); credit the trade to the first insider.
    own = own[own["RPTOWNER_RELATIONSHIP"].fillna("").str.contains("Director|Officer")]
    own = own.drop_duplicates("ACCESSION_NUMBER")
    df = tr.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER")
    out = pd.DataFrame({
        "cik": pd.to_numeric(df["ISSUERCIK"], errors="coerce"),
        "filed": pd.to_datetime(df["FILING_DATE"], format="%d-%b-%Y", errors="coerce"),
        "traded": pd.to_datetime(df["TRANS_DATE"], format="%d-%b-%Y", errors="coerce"),
        "code": df["TRANS_CODE"],
        "shares": pd.to_numeric(df["TRANS_SHARES"], errors="coerce"),
        "price": pd.to_numeric(df["TRANS_PRICEPERSHARE"], errors="coerce"),
        "owner": pd.to_numeric(df["RPTOWNERCIK"], errors="coerce"),
        "officer": df["RPTOWNER_RELATIONSHIP"].str.contains("Officer", na=False),
        "title": df["RPTOWNER_TITLE"].fillna(""),
        "plan": df["AFF10B5ONE"].fillna("0").isin(["1", "true", "True"]),
        "accession": df["ACCESSION_NUMBER"],
    })
    out["value"] = out["shares"] * out["price"]
    return out


def update() -> pd.DataFrame:
    """Trades table, rebuilt when the SEC has published a quarter we haven't parsed."""
    before = {f.name for f in DIR.glob("*_form345.zip")}
    try:
        new = {f.name for f in download()} - before
    except Exception as e:  # offline: keep what we have
        print(f"[insider] could not check for new data: {e}", flush=True)
        new = set()
    return build(refresh=bool(new) or not TRADES.exists())


def build(refresh: bool = False) -> pd.DataFrame:
    """All open-market insider trades since FIRST_YEAR, one row per transaction."""
    if TRADES.exists() and not refresh:
        return pd.read_parquet(TRADES)
    parts = [_parse(f) for f in download()]
    t = pd.concat(parts, ignore_index=True).dropna(subset=["cik", "filed", "value", "owner"])
    # Amendments and re-filings repeat trades; price 0 or absurd values are data errors.
    t = t.drop_duplicates(["cik", "owner", "traded", "code", "shares", "price"])
    t = t[(t["price"] > 0) & (t["value"] >= MIN_TRADE_USD) & (t["value"] < 5e9)]
    t = t[~((t["code"] == "S") & t["plan"])]
    t["cik"] = t["cik"].astype(int)
    t = t.sort_values("filed").reset_index(drop=True)
    t.to_parquet(TRADES)
    print(f"[insider] {len(t):,} open-market trades, {t['cik'].nunique():,} companies", flush=True)
    return t


def features(trades: pd.DataFrame, ciks: pd.Series, as_of, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Per company (index = ciks.index), insider activity filed in the `days` before as_of."""
    as_of = pd.Timestamp(as_of)
    w = trades[(trades["filed"] <= as_of) & (trades["filed"] > as_of - pd.Timedelta(days=days))]
    w = w[w["cik"].isin(set(ciks.dropna().astype(int)))]
    buys, sells = w[w["code"] == "P"], w[w["code"] == "S"]
    g = pd.DataFrame({
        "ins_buy_usd": buys.groupby("cik")["value"].sum(),
        "ins_buyers": buys.groupby("cik")["owner"].nunique(),
        "ins_officer_buyers": buys[buys["officer"]].groupby("cik")["owner"].nunique(),
        "ins_sell_usd": sells.groupby("cik")["value"].sum(),
        "ins_sellers": sells.groupby("cik")["owner"].nunique(),
        "ins_last_buy": buys.groupby("cik")["traded"].max(),
    })
    out = g.reindex(ciks.astype("Int64").values)
    out.index = ciks.index
    for c in ("ins_buy_usd", "ins_buyers", "ins_officer_buyers", "ins_sell_usd", "ins_sellers"):
        out[c] = out[c].fillna(0.0)
    out["ins_net_usd"] = out["ins_buy_usd"] - out["ins_sell_usd"]
    return out


def add(df: pd.DataFrame, trades: pd.DataFrame, ciks: pd.Series, as_of) -> pd.DataFrame:
    """Join insider features onto a cross-section and scale by market cap."""
    f = features(trades, ciks.reindex(df.index), as_of)
    df = df.drop(columns=[c for c in f.columns if c in df], errors="ignore").join(f)
    mcap = df["mcap"].where(df["mcap"] > 0)
    df["ins_net_pct"] = df["ins_net_usd"] / mcap
    df["ins_cluster"] = df["ins_buyers"] >= 2
    return df


def summary(r) -> str:
    """'3 insiders bought $1.2M' style one-liner, or '' when nothing notable."""
    b, s = r.get("ins_buyers") or 0, r.get("ins_sell_usd") or 0
    parts = []
    if b:
        parts.append(f"{int(b)} insider{'s' if b > 1 else ''} bought {_usd(r.get('ins_buy_usd'))}")
    if s and (r.get("ins_net_pct") or 0) < -0.005:
        parts.append(f"insiders sold {_usd(s)}")
    return ", ".join(parts)


def _usd(v) -> str:
    v = float(v or 0)
    return f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}K"


def _form4(cik: int, accession: str, doc: str) -> list[dict]:
    """Open-market P/S trades by officers/directors in one Form 4 XML (cached forever: filings don't change)."""
    cache = DIR / "form4" / f"{accession}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
           f"{doc.split('/')[-1]}")
    r = http.get(url, headers=_headers(), max_rps=config.SEC_MAX_RPS)
    rows = []
    if r.status_code == 200:
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            root = None
        if root is not None:
            rel = root.find("reportingOwner/reportingOwnerRelationship")
            flag = lambda tag: rel is not None and (rel.findtext(tag) or "").strip() in ("1", "true")
            owner = root.findtext("reportingOwner/reportingOwnerId/rptOwnerCik") or ""
            plan = (root.findtext("aff10b5One") or "").strip() in ("1", "true")
            if flag("isDirector") or flag("isOfficer"):
                for t in root.iter("nonDerivativeTransaction"):
                    code = t.findtext("transactionCoding/transactionCode")
                    if code not in ("P", "S"):
                        continue
                    sh = t.findtext("transactionAmounts/transactionShares/value")
                    pr = t.findtext("transactionAmounts/transactionPricePerShare/value")
                    rows.append({"code": code, "shares": float(sh or 0), "price": float(pr or 0),
                                 "traded": t.findtext("transactionDate/value"), "owner": int(owner or 0),
                                 "officer": flag("isOfficer"), "plan": plan})
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows))
    return rows


def recent(trades: pd.DataFrame, ciks: dict) -> pd.DataFrame:
    """Append Form 4s filed after the newest quarterly data set (live screens only).
    `ciks` maps ticker -> CIK for the companies worth the extra requests."""
    from . import events
    since = trades["filed"].max()
    add = []
    for t, cik in ciks.items():
        try:
            sub = events.submissions(int(cik))
        except Exception:
            continue
        new = sub[sub["form"].isin(["4", "4/A"]) & (sub["filed"] > since)]
        for _, f in new.iterrows():
            for row in _form4(int(cik), f["accession"], f["doc"]):
                add.append({**row, "cik": int(cik), "filed": f["filed"], "accession": f["accession"],
                            "title": ""})
    if not add:
        return trades
    a = pd.DataFrame(add)
    a["traded"] = pd.to_datetime(a["traded"], errors="coerce")
    a["value"] = a["shares"] * a["price"]
    a = a[(a["price"] > 0) & (a["value"] >= MIN_TRADE_USD)]
    a = a[~((a["code"] == "S") & a["plan"])]
    return pd.concat([trades, a[trades.columns.intersection(a.columns)]], ignore_index=True)

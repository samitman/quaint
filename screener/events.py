"""Corporate events from SEC 8-K filings, plus recent news headlines.

8-K item codes are the official, dated record of big announcements, so they work point-in-time
in the backtest. Headlines (Google News RSS) are context for the report only: there is no free
history to test them against, so they never affect the ranking.
"""
from __future__ import annotations

import email.utils
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import pandas as pd

from . import config, http

DIR = config.CACHE_DIR / "submissions"
MAX_AGE_DAYS = 1.0

# Items that mean "something is badly wrong": exclude for a year.
RED_FLAGS = {
    "4.02": "restatement",
    "1.03": "bankruptcy",
}
# Worth a look but often benign (a routine auditor rotation, a move from NYSE to Nasdaq).
WARNINGS = {
    "4.01": "auditor change",
    "3.01": "listing notice",
}
# Items worth knowing about recently, shown as context. (5.02 officer/director changes are left out:
# most are routine board seats and pay plans, and the item code can't tell a CEO exit apart.)
NOTABLE = {
    "1.01": "major agreement",
    "2.01": "acquisition/sale",
    "2.06": "impairment",
}
RED_FLAG_DAYS = 365
NOTABLE_DAYS = 90


def submissions(cik: int, refresh: bool = False) -> pd.DataFrame:
    """Recent filings from the SEC submissions feed (cached a day): form, filed, items, accession, doc."""
    p = DIR / f"{int(cik)}.json"
    if refresh or not p.exists() or time.time() - p.stat().st_mtime > MAX_AGE_DAYS * 86400:
        r = http.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                     headers={"User-Agent": config.require_sec_agent()}, max_rps=config.SEC_MAX_RPS)
        if r.status_code != 200:
            return pd.DataFrame(columns=["form", "filed", "items", "accession", "doc"])
        rec = r.json().get("filings", {}).get("recent", {})
        keep = {k: rec.get(k, []) for k in ("form", "filingDate", "items", "accessionNumber",
                                             "primaryDocument")}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(keep))
    rec = json.loads(p.read_text())
    n = len(rec["form"])
    return pd.DataFrame({"form": rec["form"], "filed": pd.to_datetime(rec["filingDate"]),
                         "items": rec["items"], "accession": rec.get("accessionNumber", [""] * n),
                         "doc": rec.get("primaryDocument", [""] * n)})


def filings_8k(cik: int, refresh: bool = False) -> pd.DataFrame:
    """8-K / 8-K/A filings: filed date and item codes."""
    df = submissions(cik, refresh)
    return df[df["form"].isin(["8-K", "8-K/A"])][["filed", "items"]].reset_index(drop=True)


def summarize(f8k: pd.DataFrame, as_of) -> dict:
    """Red flags in the last year and notable events in the last 90 days, as of a date."""
    as_of = pd.Timestamp(as_of)
    past = f8k[f8k["filed"] <= as_of]
    out = {"red_flags": "", "warnings": "", "events": ""}
    if past.empty:
        return out
    age = (as_of - past["filed"]).dt.days
    items = past["items"].fillna("").str.split(",")
    red, warn, notable = [], [], []
    for its, a in zip(items, age):
        for it in its:
            it = it.strip()
            if it in RED_FLAGS and a <= RED_FLAG_DAYS and RED_FLAGS[it] not in red:
                red.append(RED_FLAGS[it])
            if it in WARNINGS and a <= RED_FLAG_DAYS and WARNINGS[it] not in warn:
                warn.append(WARNINGS[it])
            if it in NOTABLE and a <= NOTABLE_DAYS and NOTABLE[it] not in notable:
                notable.append(NOTABLE[it])
    out["red_flags"], out["warnings"], out["events"] = ", ".join(red), ", ".join(warn), ", ".join(notable)
    return out


def add(df: pd.DataFrame, ciks: pd.Series, as_of, tickers=None) -> pd.DataFrame:
    """Join red_flags / warnings / events onto a cross-section (submissions for `tickers` or all)."""
    df = df.copy()
    df["red_flags"], df["warnings"], df["events"] = "", "", ""
    for t in (tickers if tickers is not None else df.index):
        c = ciks.get(t)
        if pd.isna(c):
            continue
        try:
            s = summarize(filings_8k(int(c)), as_of)
        except Exception:
            continue
        for k, v in s.items():
            df.at[t, k] = v
    return df


# Auto-generated filler (insider-trade and fund-holding bulletins) crowds out real news.
_SPAM_SOURCES = {"MarketBeat", "Defense World", "ETF Daily News", "Ticker Report", "American Banking News",
                 "The Lincolnian Online", "Modern Readers", "Dakota Financial News", "Stock Titan",
                 "Investing.com Canada", "Investing.com India", "Insider Monkey", "CryptoRank", "Bybit",
                 "Stock Traders Daily", "Moneycontrol.com", "Moneycontrol"}
_SPAM = re.compile(r"Form 4|Form 144|\binsider\b|shares (sold|bought|acquired)|\b(sells|buys|acquires)\b.*shares|"
                   r"stake|position in|holdings in|short interest|\bSEC filing\b|options chain|stock forecasts?|historical prices"
                   r"|stock price, news|quote (&|and) history|tokenized|price prediction|advanced charts"
                   r"|dividend analysis|technicals|risk zones|momentum shifts|\b(adds|reduces|trims|boosts)\b.*\bshares"
                   r"|stock (looks|down|up) |facts to note|\b(ascends|descends|outpaces|lags)\b", re.I)


def headlines(name: str, ticker: str, n: int = 3, days: int = 30) -> list[dict]:
    """Latest news headlines about a company (Google News RSS): title, source, date, link."""
    q = urllib.parse.quote(f'"{name}" OR "{ticker} stock" when:{days}d')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        root = ET.fromstring(http.get(url, max_rps=2.0, timeout=15).content)
    except Exception:
        return []
    out = []
    for it in root.iter("item"):
        src = it.find("source")
        title = it.findtext("title") or ""
        source = src.text if src is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            when = pd.Timestamp(email.utils.parsedate_to_datetime(it.findtext("pubDate"))).tz_convert(None)
        except Exception:
            when = pd.NaT
        if source in _SPAM_SOURCES or _SPAM.search(title):
            continue
        out.append({"title": title, "source": source, "date": when, "link": it.findtext("link") or ""})
    out.sort(key=lambda h: h["date"] if pd.notna(h["date"]) else pd.Timestamp(0), reverse=True)
    return out[:n]

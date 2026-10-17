"""SEC EDGAR XBRL 'companyfacts' loader with on-disk cache.

One request per company returns every XBRL fact it has ever filed, each tagged with
the date it was filed. That `filed` date is what makes point-in-time backtests honest:
we only use facts filed on or before the as-of date.
"""
from __future__ import annotations

import gzip
import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, http

URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def _path(cik: int):
    return config.EDGAR_CACHE / f"CIK{cik:010d}.json.gz"


def _fresh(p, max_age_days: float) -> bool:
    return p.exists() and (time.time() - p.stat().st_mtime) < max_age_days * 86400


# Holding-company reorganisations create a new CIK with no history; the old registrant
# keeps everything before the switch. Map successor -> predecessors and merge them.
# `python -m screener audit` lists companies with suspiciously short histories.
PREDECESSORS: dict[int, list[int]] = {
    2115436: [34088],  # ExxonMobil Holdings Corp <- Exxon Mobil Corp (2026)
}


def fetch(cik: int, *, refresh: bool = False) -> dict | None:
    cf = _fetch_one(cik, refresh=refresh)
    for old in PREDECESSORS.get(cik, []):
        prev = _fetch_one(old, refresh=refresh)
        if cf and prev:
            cf = merge(cf, prev)
    return cf


def merge(new: dict, old: dict) -> dict:
    """Union of facts; `filed` dates keep point-in-time logic correct across the seam."""
    out = {"cik": new.get("cik"), "entityName": new.get("entityName"), "facts": {}}
    for src in (old, new):
        for ns, concepts in src.get("facts", {}).items():
            dst = out["facts"].setdefault(ns, {})
            for c, node in concepts.items():
                d = dst.setdefault(c, {"units": {}})
                for u, facts in node["units"].items():
                    d["units"].setdefault(u, []).extend(facts)
    return out


def _fetch_one(cik: int, *, refresh: bool = False, load: bool = True) -> dict | None:
    p = _path(cik)
    if not load and not refresh and _fresh(p, config.EDGAR_MAX_AGE_DAYS):
        return {}
    if not refresh and _fresh(p, config.EDGAR_MAX_AGE_DAYS):
        with gzip.open(p, "rt") as f:
            return json.load(f)
    r = http.get(URL.format(cik=cik), headers={"User-Agent": config.require_sec_agent()},
                 max_rps=config.SEC_MAX_RPS)
    if r.status_code == 404:
        return None
    data = r.json()
    config.EDGAR_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(data, f)
    tmp.replace(p)
    return data


def fetch_many(ciks, *, refresh: bool = False, workers: int = 4) -> list[int]:
    """Warm the cache; returns the CIKs that failed. Payloads are not kept in memory
    (a parsed companyfacts file can be 100MB+ of Python objects)."""
    ciks = sorted(set(int(c) for c in ciks))
    failed = []

    def one(c):
        try:
            return c, _fetch_one(c, refresh=refresh, load=False) is not None
        except Exception as e:  # one bad filer must not kill the run
            print(f"[edgar] CIK {c}: {e}")
            return c, False

    ciks += [o for c in ciks for o in PREDECESSORS.get(c, [])]
    with ThreadPoolExecutor(workers) as ex:
        for i, (c, ok) in enumerate(ex.map(one, ciks), 1):
            if not ok:
                failed.append(c)
            if i % 100 == 0:
                print(f"[edgar] {i}/{len(ciks)}")
    return failed

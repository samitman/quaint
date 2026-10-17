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


def fetch(cik: int, *, refresh: bool = False) -> dict | None:
    p = _path(cik)
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


def fetch_many(ciks, *, refresh: bool = False, workers: int = 4) -> dict[int, dict]:
    ciks = sorted(set(int(c) for c in ciks))
    out: dict[int, dict] = {}

    def one(c):
        try:
            return c, fetch(c, refresh=refresh)
        except Exception as e:  # one bad filer must not kill the run
            print(f"[edgar] CIK {c}: {e}")
            return c, None

    with ThreadPoolExecutor(workers) as ex:
        for i, (c, d) in enumerate(ex.map(one, ciks), 1):
            if d:
                out[c] = d
            if i % 100 == 0:
                print(f"[edgar] {i}/{len(ciks)}")
    return out

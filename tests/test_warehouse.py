"""The warehouse must give back exactly what the filings give: stored facts rebuild the same
snapshots, stored snapshots decode to the same inputs. Postgres parts run only with DATABASE_URL."""
import json
import os

import pandas as pd
import pytest

from screener import db, fundamentals as F, warehouse as W

from test_fundamentals import CFO, cf, fact

REV = [fact("2023-01-01", "2023-03-31", 100, "2023-05-01"), fact("2023-04-01", "2023-06-30", 110, "2023-08-01"),
       fact("2023-07-01", "2023-09-30", 120, "2023-11-01"), fact("2023-01-01", "2023-12-31", 460, "2024-02-15", "10-K"),
       fact("2024-01-01", "2024-03-31", 130, "2024-05-01"),
       fact("2024-01-01", "2024-03-31", 999, "2024-05-02", "8-K")]  # non-periodic: never used
COMPANY = cf({"Revenues": REV, "NetCashProvidedByUsedInOperatingActivities": CFO,
              "Assets": [fact(None, "2023-12-31", 900, "2024-02-15", "10-K"), fact(None, "2024-03-31", 950, "2024-05-01")]},
             {"EntityCommonStockSharesOutstanding": [fact(None, "2024-04-20", 50, "2024-05-01")]})


def canon(v):
    return json.dumps(db._enc(v), sort_keys=True)


def test_codec_round_trip():
    v = F.snapshot(COMPANY).values
    back = db.loads(db.dumps(v))
    assert set(back) == set(v)
    for k in v:
        assert db.dumps({k: back[k]}) == db.dumps({k: v[k]}), k
    assert isinstance(back["period_end"], pd.Timestamp)
    assert list(back["revenue_annual"]) == list(v["revenue_annual"])


def test_stored_facts_rebuild_identical_snapshots():
    rows = W.fact_rows(1, COMPANY)
    assert all(r[9] in F.FORMS for r in rows)  # the 8-K fact is dropped
    stored = W.cf_from_rows(rows)
    for d in ["2023-06-01", "2023-12-01", "2024-03-01", "2024-06-01"]:
        assert db.dumps(F.snapshot(stored, as_of=d).values) == db.dumps(F.snapshot(COMPANY, as_of=d).values), d


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_versions_cover_every_filing_date(monkeypatch):
    monkeypatch.setattr(W.edgar, "fetch", lambda cik: COMPANY)
    db.init()
    cik = 999_999_999
    try:
        W.sync_company(cik, rebuild=True)
        vers = db.read("select filed, inputs from snapshots where cik = %s order by filed", (cik,))
        filed = sorted({r[8] for r in W.fact_rows(cik, COMPANY)})
        assert [f.isoformat() for f in vers["filed"]] == filed
        for f, inputs in zip(vers["filed"], vers["inputs"]):
            want = F.snapshot(COMPANY, as_of=pd.Timestamp(f)).values
            assert canon(db.loads(inputs)) == canon(want)
    finally:
        with db.connect() as con:
            for t in ("facts", "snapshots", "fact_loads"):
                con.execute(f"delete from {t} where cik = %s", (cik,))

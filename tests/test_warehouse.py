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


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_changes_between_runs():
    db.init()
    row = lambda t, top, ok, failed=(), px=10.0: {"t": t, "top": top, "pass": ok, "fin": False, "rank": 1 if ok else None,
                                                  "failed": list(failed), "px": px, "ir": .2, "pend": "2026-06-30"}
    runs = {"2099-01-02": [row("AAA", False, False, ["mcap"]), row("BBB", True, True), row("CCC", False, True)],
            "2099-01-03": [row("AAA", True, True, px=9.0), row("BBB", False, True), row("CCC", False, False, ["net_margin"])]}
    ids = []
    try:
        with db.connect() as con:
            for d, rows in runs.items():
                rid = con.execute("insert into screen_runs (as_of, method, n) values (%s, 'test', 0) returning id", (d,)).fetchone()[0]
                ids.append(rid)
                for r in rows:
                    con.execute("insert into screen_rows (run_id, ticker, passes, investable, data) values (%s, %s, %s, true, %s)",
                                (rid, r["t"], r["pass"], json.dumps(r)))
        c = W.changes(ids[-1])
        assert c["since"] == "2099-01-02"
        got = {i["t"]: i for i in c["items"]}
        assert got["AAA"]["kind"] == "in_top" and got["AAA"]["cleared"] == ["mcap"] and round(got["AAA"]["px_chg"], 2) == -.1
        assert got["BBB"]["kind"] == "out_top" and got["CCC"]["kind"] == "out_pass" and got["CCC"]["failing"] == ["net_margin"]
        assert [i["t"] for i in c["items"]] == ["AAA", "BBB", "CCC"]
    finally:
        with db.connect() as con:
            con.execute("delete from screen_runs where id = any(%s)", (ids,))

"""Load our data into Postgres and read it back fast (see db.py for the tables).

Two layers make history cheap to reuse:
  * facts: every tracked XBRL fact with the date it was filed. Any code that wants a
    company's filings rebuilds the companyfacts shape from here (`load_cf`).
  * snapshots: `fundamentals.snapshot` depends on the as-of date only through "filed on or
    before", so it only changes when a filing lands. We store it once per filing date and a
    cross-section for any date is a lookup plus `metrics.compute`: minutes, not a rebuild.
    Versions are tagged with a hash of fundamentals.py; if that logic changes, `sync --rebuild`
    recomputes them. Changes to metrics or scoring need no rebuild at all.
"""
from __future__ import annotations

import bisect
import hashlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from . import db, edgar, fundamentals as F

TRACKED = sorted(set(sum(F.FLOW.values(), []) + sum(F.INSTANT.values(), []) + ["EntityCommonStockSharesOutstanding"]))
UNITS = ("USD", "shares", "USD/shares")
LOGIC = hashlib.sha1(Path(F.__file__).read_bytes()).hexdigest()[:12]
FACT_COLS = ["cik", "ns", "concept", "unit", "seq", "start_date", "end_date", "val", "filed", "form", "fy", "fp", "accn"]


# ---------- facts ----------

def fact_rows(cik: int, cf: dict) -> list[tuple]:
    want = set(TRACKED)
    rows = []
    for ns, concepts in cf.get("facts", {}).items():
        for c, node in concepts.items():
            if c not in want:
                continue
            for u, facts in node.get("units", {}).items():
                if u not in UNITS:
                    continue
                for i, f in enumerate(facts):
                    if f.get("form") not in F.FORMS:  # only periodic reports are ever used
                        continue
                    rows.append((cik, ns, c, u, i, f.get("start"), f["end"], float(f["val"]), f["filed"],
                                 f.get("form"), f.get("fy"), f.get("fp"), f.get("accn")))
    return rows


def cf_from_rows(rows) -> dict:
    """companyfacts-shaped dict from fact rows (same order as SEC's file)."""
    out: dict = {"facts": {}}
    for cik, ns, c, u, seq, st, en, val, filed, form, fy, fp, accn in sorted(rows, key=lambda r: r[1:5]):
        f = {"end": str(en), "val": val, "filed": str(filed), "form": form, "fy": fy, "fp": fp, "accn": accn}
        if st is not None:
            f["start"] = str(st)
        out["facts"].setdefault(ns, {}).setdefault(c, {"units": {}})["units"].setdefault(u, []).append(f)
    return out


def load_cf(cik: int) -> dict | None:
    with db.connect() as con:
        rows = con.execute(f"select {', '.join(FACT_COLS)} from facts where cik = %s", (int(cik),)).fetchall()
    return cf_from_rows(rows) if rows else None


# ---------- one company: facts + point-in-time versions ----------

def sync_company(cik: int, rebuild: bool = False) -> tuple[int, int]:
    """Store a company's facts and any missing snapshot versions. Returns (facts, new versions)."""
    cf = edgar.fetch(int(cik))
    if not cf:
        return 0, 0
    rows = fact_rows(int(cik), cf)
    tracked = cf_from_rows(rows)
    filed = sorted({r[8] for r in rows})
    with db.connect() as con:
        with con.transaction():
            con.execute("delete from facts where cik = %s", (int(cik),))
            db.copy_rows(con, "facts", FACT_COLS, rows)
            if rebuild:
                con.execute("delete from snapshots where cik = %s", (int(cik),))
            else:
                con.execute("delete from snapshots where cik = %s and logic <> %s", (int(cik), LOGIC))
            have = {r[0].isoformat() for r in con.execute("select filed from snapshots where cik = %s", (int(cik),))}
        todo = [d for d in filed if d not in have]
        new = [(int(cik), d, LOGIC, db.dumps(F.snapshot(tracked, as_of=pd.Timestamp(d)).values)) for d in todo]
        if new:
            db.copy_rows(con, "snapshots", ["cik", "filed", "logic", "inputs"], new)
        # Marked loaded only once its versions are in, so an interrupted load is redone next time.
        con.execute("insert into fact_loads (cik, n, last_filed) values (%s, %s, %s) on conflict (cik) do update "
                    "set loaded_at = now(), n = excluded.n, last_filed = excluded.last_filed",
                    (int(cik), len(rows), filed[-1] if filed else None))
    return len(rows), len(new)


def _sync_one(args):
    cik, rebuild = args
    try:
        return cik, *sync_company(cik, rebuild)
    except Exception as e:  # one bad filer shouldn't stop the load
        print(f"[warehouse] CIK {cik}: {e}", flush=True)
        return cik, 0, 0


def sync_fundamentals(ciks, workers: int = 4, rebuild: bool = False) -> None:
    ciks = sorted({int(c) for c in ciks if pd.notna(c)})
    nf = nv = 0
    with ProcessPoolExecutor(workers) as ex:
        for i, (_, f, v) in enumerate(ex.map(_sync_one, [(c, rebuild) for c in ciks], chunksize=4), 1):
            nf, nv = nf + f, nv + v
            if i % 100 == 0 or i == len(ciks):
                print(f"[warehouse] {i}/{len(ciks)} companies, {nf:,} facts, {nv:,} new versions", flush=True)


# ---------- companies, prices, insider trades ----------

def sync_companies(uni: pd.DataFrame) -> None:
    u = uni.reset_index() if "ticker" not in uni.columns else uni
    rows = [(r.ticker, int(r.cik) if pd.notna(r.cik) else None, r.sec_name, r.sector, r.industry, r["index"],
             r.exchange, r.hq, bool(r.us_hq) if pd.notna(r.us_hq) else None) for _, r in u.iterrows()]
    with db.connect() as con, con.transaction():
        con.execute("delete from companies")
        db.copy_rows(con, "companies", ["ticker", "cik", "name", "sector", "industry", "idx", "exchange", "hq", "us_hq"], rows)


def load_companies() -> pd.DataFrame:
    df = db.read("select ticker, cik, name as sec_name, sector, industry, idx as index, exchange, hq, us_hq from companies")
    return df.set_index("ticker")


def sync_prices(px: dict, tickers=None) -> int:
    """Upsert prices. Yahoo restates a ticker's whole history after a split, so callers
    pass the full history for tickers whose splits changed."""
    close = px["Close"] if tickers is None else px["Close"][[t for t in tickers if t in px["Close"]]]
    long = close.stack().rename("close").to_frame()
    for f, c in (("Adj Close", "adj_close"), ("Volume", "volume"), ("Stock Splits", "split")):
        long[c] = px[f][close.columns].stack().reindex(long.index)
    long = long.reset_index()
    long.columns = ["d", "ticker", "close", "adj_close", "volume", "split"]
    with db.connect() as con, con.transaction():
        con.execute("create temp table px_in (like prices) on commit drop")
        n = db.copy_rows(con, "px_in", ["ticker", "d", "close", "adj_close", "volume", "split"],
                         ((r.ticker, r.d.date(), _f(r.close), _f(r.adj_close), _f(r.volume), _f(r.split))
                          for r in long.itertuples()))
        con.execute("insert into prices select * from px_in on conflict (ticker, d) do update set close = excluded.close, "
                    "adj_close = excluded.adj_close, volume = excluded.volume, split = excluded.split")
    return n


def _f(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def load_prices(tickers=None, start=None) -> dict[str, pd.DataFrame]:
    """Same shape as prices.load: {'Close', 'Adj Close', 'Volume', 'Stock Splits'} -> dates x tickers."""
    q, p = "select ticker, d, close, adj_close, volume, split from prices where true", []
    if tickers is not None:
        q += " and ticker = any(%s)"
        p.append(list(tickers))
    if start is not None:
        q += " and d >= %s"
        p.append(pd.Timestamp(start).date())
    df = db.read(q, p, parse_dates=["d"])
    out = {}
    for f, c in (("Close", "close"), ("Adj Close", "adj_close"), ("Volume", "volume"), ("Stock Splits", "split")):
        w = df.pivot(index="d", columns="ticker", values=c).sort_index()
        w.index.name, w.columns.name = None, None
        out[f] = w
    if tickers is not None:  # keep requested order; unknown tickers become empty columns
        out = {f: w.reindex(columns=list(tickers)) for f, w in out.items()}
    return out


def sync_trades(trades: pd.DataFrame) -> None:
    cols = ["cik", "filed", "traded", "code", "shares", "price", "owner", "officer", "title", "plan", "accession", "value"]
    t = trades[cols].astype(object).where(trades[cols].notna(), None)
    with db.connect() as con, con.transaction():
        con.execute("delete from trades")
        db.copy_rows(con, "trades", cols, (tuple(r) for r in t.itertuples(index=False)))


def load_trades(ciks=None) -> pd.DataFrame:
    q, p = "select * from trades", None
    if ciks is not None:
        q, p = q + " where cik = any(%s)", ([int(c) for c in ciks],)
    return db.read(q, p, parse_dates=["filed", "traded"])


# ---------- cross-sections from stored versions ----------

def _versions(ciks) -> dict[int, tuple[list, list]]:
    out: dict[int, tuple[list, list]] = {}
    with db.connect() as con:
        cur = con.execute("select cik, filed, inputs from snapshots where logic = %s and cik = any(%s) order by cik, filed",
                          (LOGIC, [int(c) for c in ciks]))
        for cik, filed, inputs in cur:
            ds, vs = out.setdefault(int(cik), ([], []))
            ds.append(pd.Timestamp(filed))
            vs.append(inputs)
    return out


_X: dict = {}


def _xs_init(uni, px, vers):
    _X["uni"], _X["px"], _X["vers"] = uni, px, vers


_EMPTY_SNAPSHOT: dict = {}


def _xs_company(cik):
    from . import screen
    ds, raw = _X["vers"].get(cik, ([], []))
    if not ds:  # no filings at all (as screen.cross_sections: no rows)
        return []
    cache: dict[int, dict] = {}

    def snap_at(d):
        i = bisect.bisect_right(ds, d) - 1
        if i < 0:  # trading before its first filing: no fundamentals yet, same as parsing nothing
            if not _EMPTY_SNAPSHOT:
                _EMPTY_SNAPSHOT.update(F.snapshot({"facts": {}}).values)
            return dict(_EMPTY_SNAPSHOT)
        if i not in cache:
            cache[i] = db.loads(raw[i])
        return cache[i]
    uni = _X["uni"]
    return screen.company_rows(uni[uni["cik"] == cik], _X["px"], _X["dates"], snap_at)


def _xs_run(args):
    cik, dates = args
    _X["dates"] = dates
    return _xs_company(cik)


def cross_sections(dates, uni: pd.DataFrame, px: dict | None = None, workers: int = 4) -> pd.DataFrame:
    """Same output as screen.cross_sections (historical mode), from stored versions."""
    from . import screen
    uni = uni.set_index("ticker") if "ticker" in uni.columns else uni
    px = load_prices(uni.index.tolist()) if px is None else px
    ciks = [int(c) for c in uni["cik"].dropna().unique()]
    vers = _versions(ciks)
    dates = [pd.Timestamp(d) for d in dates]
    rows = []
    with ProcessPoolExecutor(workers, initializer=_xs_init, initargs=(uni, px, vers)) as ex:
        for part in ex.map(_xs_run, [(c, dates) for c in ciks], chunksize=8):
            rows.extend(part)
    return screen.add_adv(pd.DataFrame(rows), px)


def data_version() -> str:
    """Changes whenever new filings or prices land: part of the backtest cache key."""
    with db.connect() as con:
        a = con.execute("select coalesce(max(loaded_at)::text, ''), count(*) from fact_loads").fetchone()
        b = con.execute("select coalesce(max(d)::text, '') from prices").fetchone()
    return hashlib.sha1(f"{a}{b}{LOGIC}".encode()).hexdigest()[:12]


# ---------- screen runs ----------

def method_version() -> str:
    """Hash of the code that decides the list, stored with every run."""
    from . import metrics, score
    code = b"".join(Path(m.__file__).read_bytes() for m in (F, metrics, score))
    return hashlib.sha1(code).hexdigest()[:12]


def record_screen(scored: pd.DataFrame, news: dict | None = None) -> int:
    """Store a live screen: every company's app row (what the index/search/screen views show)."""
    import json
    from . import appdata
    uni = universe_frame()
    rows = appdata.index_rows(scored, uni)
    d = pd.Timestamp(scored["date"].iloc[0]).date()
    with db.connect() as con, con.transaction():
        rid = con.execute("insert into screen_runs (as_of, method, n) values (%s, %s, %s) returning id",
                          (d, method_version(), int(scored["passes"].sum()))).fetchone()[0]
        db.copy_rows(con, "screen_rows", ["run_id", "ticker", "passes", "investable", "composite", "data"],
                     ((rid, r["t"], r["pass"], r["inv"], _f(scored.at[r["t"], "composite"]), json.dumps(r))
                      for r in rows))
        if news:
            con.execute("insert into meta (key, value) values ('news', %s) on conflict (key) do update "
                        "set value = excluded.value, updated_at = now()", (json.dumps(appdata.news_rows(news)),))
    return rid


def universe_frame() -> pd.DataFrame:
    from . import universe
    return universe.load().set_index("ticker")


def latest_index() -> dict | None:
    """index.json equivalent from the latest stored run."""
    import json
    with db.connect() as con:
        r = con.execute("select id, as_of from screen_runs order by id desc limit 1").fetchone()
        if not r:
            return None
        rows = [x[0] for x in con.execute("select data from screen_rows where run_id = %s", (r[0],))]
        nw = con.execute("select value from meta where key = 'news'").fetchone()
    return {"asof": r[1].isoformat(), "run": r[0], "rows": rows, "news": json.loads(nw[0]) if nw else {},
            "changes": changes(r[0])}


def changes(run_id: int) -> dict | None:
    """What moved between this run and the last run on an earlier price date: names that entered
    or left the top 25 or the passing list, with what changed (price, filing, gates)."""
    with db.connect() as con:
        cur = con.execute("select as_of from screen_runs where id = %s", (run_id,)).fetchone()
        prev = con.execute("select id, as_of from screen_runs where as_of < %s order by as_of desc, id desc limit 1",
                           (cur[0],)).fetchone() if cur else None
        if not prev:
            return None
        now = {x[0]["t"]: x[0] for x in con.execute("select data from screen_rows where run_id = %s", (run_id,))}
        was = {x[0]["t"]: x[0] for x in con.execute("select data from screen_rows where run_id = %s", (prev[0],))}
    items = []
    for t, r in now.items():
        p = was.get(t)
        if p is None:
            continue  # new to the universe: no "before" to compare
        if r["top"] and not p["top"]:
            kind = "in_top"
        elif r["pass"] and not p["pass"]:
            kind = "in_pass"
        elif p["top"] and not r["top"]:
            kind = "out_top"
        elif p["pass"] and not r["pass"]:
            kind = "out_pass"
        else:
            continue
        px = r.get("px") / p["px"] - 1 if r.get("px") and p.get("px") else None
        items.append({"t": t, "kind": kind, "fin": r["fin"], "rank": r.get("rank"), "px_chg": px,
                      "ir": r.get("ir"), "ir_prev": p.get("ir"),
                      "cleared": [g for g in p.get("failed", []) if g not in r.get("failed", [])],
                      "failing": [g for g in r.get("failed", []) if g not in p.get("failed", [])],
                      "filing": r.get("pend") if r.get("pend") and r.get("pend") != p.get("pend") else None})
    order = {"in_top": 0, "in_pass": 1, "out_top": 2, "out_pass": 3}
    items.sort(key=lambda x: (order[x["kind"]], x["rank"] or 999))
    return {"since": prev[1].isoformat(), "items": items}


def stale_ciks(ciks) -> list[int]:
    """Companies whose downloaded filings are newer than what the warehouse holds, or whose
    load never finished."""
    loaded = db.read("select cik, extract(epoch from loaded_at) as t from fact_loads").set_index("cik")["t"].to_dict()
    with db.connect() as con:  # facts filed on a date with no stored version (interrupted load, logic change)
        gaps = {r[0] for r in con.execute(
            "select distinct f.cik from (select distinct cik, filed from facts) f left join snapshots s "
            "on s.cik = f.cik and s.filed = f.filed and s.logic = %s where s.cik is null", (LOGIC,))}
    for c in gaps:
        loaded.pop(c, None)
    out = []
    for c in sorted({int(c) for c in ciks if pd.notna(c)}):
        p = edgar._path(c)
        if p.exists() and (c not in loaded or p.stat().st_mtime > float(loaded[c])):
            out.append(c)
    return out

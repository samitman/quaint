"""Postgres warehouse: connection, schema and a JSON codec for snapshot inputs.

Set DATABASE_URL (e.g. postgresql://quaint:quaint@localhost:5432/quaint). Tables:
  companies     universe membership, sector, CIK
  facts         every tracked XBRL fact a company filed, with its filing date (raw layer)
  snapshots     the fundamentals inputs as of each filing date (point-in-time versions)
  prices        daily close / adjusted close / volume / splits
  trades        insider open-market trades (Form 4)
  screen_runs   one row per live screen; screen_rows holds every company's numbers
  books         the monthly paper portfolio (forward scorecard)
  backtests     cached backtest results, keyed by method + data version
  meta          small key/value state (e.g. when prices were last refreshed)
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os

import numpy as np
import pandas as pd

URL = os.environ.get("DATABASE_URL", "")

SCHEMA = """
create table if not exists companies (
  ticker text primary key, cik bigint, name text, sector text, industry text, idx text,
  exchange text, hq text, us_hq boolean, updated_at timestamptz default now());
create table if not exists facts (
  cik bigint not null, ns text not null, concept text not null, unit text not null, seq int not null,
  start_date date, end_date date not null, val double precision not null, filed date not null,
  form text, fy int, fp text, accn text);
create index if not exists facts_cik on facts (cik, concept);
create table if not exists fact_loads (
  cik bigint primary key, loaded_at timestamptz default now(), n int, last_filed date);
create table if not exists snapshots (
  cik bigint not null, filed date not null, logic text not null, inputs jsonb not null,
  primary key (cik, filed));
create table if not exists prices (
  ticker text not null, d date not null, close double precision, adj_close double precision,
  volume double precision, split double precision, primary key (ticker, d));
create table if not exists trades (
  cik bigint, filed date, traded date, code text, shares double precision, price double precision,
  owner bigint, officer boolean, title text, plan boolean, accession text, value double precision);
create index if not exists trades_cik on trades (cik, traded);
create table if not exists screen_runs (
  id serial primary key, run_at timestamptz default now(), as_of date not null, method text, n int);
create table if not exists screen_rows (
  run_id int references screen_runs(id) on delete cascade, ticker text, passes boolean,
  investable boolean, composite double precision, data jsonb, primary key (run_id, ticker));
create table if not exists books (
  start date not null, ticker text not null, weight double precision, entry_price double precision,
  rank int, implied_return double precision, primary key (start, ticker));
create table if not exists meta (key text primary key, value text, updated_at timestamptz default now());
create table if not exists backtests (
  key text primary key, created_at timestamptz default now(), method text, data_version text,
  result bytea);
"""


def enabled() -> bool:
    return bool(URL)


def connect():
    import psycopg
    if not URL:
        raise SystemExit("Set DATABASE_URL, e.g. postgresql://quaint:quaint@localhost:5432/quaint")
    return psycopg.connect(URL, autocommit=True)


def init() -> None:
    with connect() as con:
        con.execute(SCHEMA)


def read(sql: str, params=None, parse_dates=None) -> pd.DataFrame:
    with connect() as con:
        cur = con.execute(sql, params)
        cols = [c.name for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    for c in parse_dates or []:
        df[c] = pd.to_datetime(df[c])
    return df


def copy_rows(con, table: str, cols: list[str], rows) -> int:
    n = 0
    with con.cursor().copy(f"copy {table} ({', '.join(cols)}) from stdin") as cp:
        for r in rows:
            cp.write_row(r)
            n += 1
    return n


# ---- JSON codec: snapshot inputs hold Timestamps and {Timestamp: float} dicts ----

def _enc(v):
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date, np.datetime64)):
        return None if pd.isna(v) else {"$t": pd.Timestamp(v).isoformat()}
    if isinstance(v, dict):
        if v and not all(isinstance(k, str) for k in v):
            return {"$m": [[_enc(k), _enc(x)] for k, x in v.items()]}
        return {k: _enc(x) for k, x in v.items()}
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return {"$f": repr(f)} if math.isnan(f) or math.isinf(f) else f
    if v is pd.NaT:
        return None
    raise TypeError(f"can't store {type(v)}")


def _dec(v):
    if isinstance(v, dict):
        if "$t" in v:
            return pd.Timestamp(v["$t"])
        if "$f" in v:
            return float(v["$f"])
        if "$m" in v:
            return {_dec(k): _dec(x) for k, x in v["$m"]}
        return {k: _dec(x) for k, x in v.items()}
    return v


def dumps(v: dict) -> str:
    return json.dumps(_enc(v), separators=(",", ":"))


def loads(s) -> dict:
    return _dec(json.loads(s) if isinstance(s, (str, bytes)) else s)


def get_meta(key: str, max_age_hours: float | None = None) -> str | None:
    q = "select value from meta where key = %s"
    if max_age_hours is not None:
        q += f" and updated_at > now() - interval '{float(max_age_hours)} hours'"
    with connect() as con:
        r = con.execute(q, (key,)).fetchone()
    return r[0] if r else None


def set_meta(key: str, value: str) -> None:
    with connect() as con:
        con.execute("insert into meta (key, value) values (%s, %s) on conflict (key) do update "
                    "set value = excluded.value, updated_at = now()", (key, value))

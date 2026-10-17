"""Persistent store in the project's shared folder (ROOT/db), so data outlives a session.

Working caches stay on fast local disk (SCREENER_DATA); this module mirrors them:
  db/edgar/*.json.gz      raw SEC companyfacts (one file per company)
  db/hq/*.txt             HQ location per CIK
  db/prices/*.parquet     daily Close / Adj Close / Volume / Stock Splits (dates x tickers)
  db/universe.csv         index membership, sector, CIK
  db/screener.sqlite      every screen run (table `screens`) + backtest history (`history`)
  db/trades.parquet       insider open-market trades (Form 4) since 2016

`pull()` seeds an empty local cache from db/ (minutes instead of a full re-download);
`push()` copies anything newer back. SQLite is written locally and copied, because
SQLite locking is unreliable on network filesystems.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pandas as pd

from . import config

DB = config.ROOT / "db"
LOCAL_SQLITE = config.DATA_DIR / "screener.sqlite"
INSIDER = config.CACHE_DIR / "insider" / "trades.parquet"  # Form 4 open-market trades since 2016
_DIRS = {"edgar": config.EDGAR_CACHE, "hq": config.CACHE_DIR / "hq"}


def _sync(src: Path, dst: Path) -> int:
    """Copy files in src that are missing or newer in dst. Returns files copied."""
    if not src.exists():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.iterdir():
        if not f.is_file() or f.suffix == ".tmp":
            continue
        t = dst / f.name
        if not t.exists() or f.stat().st_mtime > t.stat().st_mtime + 1:
            shutil.copy2(f, t)
            n += 1
    return n


def push() -> None:
    n = sum(_sync(local, DB / name) for name, local in _DIRS.items())
    for f in ("Close", "Adj Close", "Volume", "Stock Splits"):
        csv = config.PRICE_CACHE / f"{f.replace(' ', '_').lower()}.csv"
        pq = DB / "prices" / f"{f.replace(' ', '_').lower()}.parquet"
        if csv.exists() and (not pq.exists() or csv.stat().st_mtime > pq.stat().st_mtime):
            pq.parent.mkdir(parents=True, exist_ok=True)
            pd.read_csv(csv, index_col=0, parse_dates=True).to_parquet(pq)
            n += 1
    for f in (config.UNIVERSE_FILE, LOCAL_SQLITE, INSIDER):
        if f.exists():
            n += _copy_if_newer(f, DB / f.name)
    print(f"[store] pushed {n} files to {DB}")


def pull() -> None:
    n = sum(_sync(DB / name, local) for name, local in _DIRS.items())
    for pq in (DB / "prices").glob("*.parquet") if (DB / "prices").exists() else []:
        csv = config.PRICE_CACHE / f"{pq.stem}.csv"
        if not csv.exists() or pq.stat().st_mtime > csv.stat().st_mtime:
            csv.parent.mkdir(parents=True, exist_ok=True)
            pd.read_parquet(pq).to_csv(csv)
            n += 1
    for name, local in (("universe.csv", config.UNIVERSE_FILE), ("screener.sqlite", LOCAL_SQLITE),
                        ("trades.parquet", INSIDER)):
        if (DB / name).exists():
            n += _copy_if_newer(DB / name, local)
    print(f"[store] pulled {n} files from {DB}")


def _copy_if_newer(src: Path, dst: Path) -> int:
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return 1


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].map(lambda v: v if v is None or isinstance(v, (str, int, float, bool))
                                else str(v))
    return out


def save_screen(scored: pd.DataFrame, columns: list[str]) -> None:
    """Append one screen run, so picks can be tracked forward (live, out-of-sample)."""
    df = scored.loc[:, ~scored.columns.duplicated()].copy()
    df["ticker"] = scored.index
    df = df[[c for c in dict.fromkeys(["ticker"] + columns) if c in df.columns]].reset_index(drop=True)
    df.insert(0, "run_at", pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"))
    LOCAL_SQLITE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(LOCAL_SQLITE) as con:
        have = {r[1] for r in con.execute("pragma table_info(screens)")}
        for c in df.columns if have else []:  # report columns get added over time
            if c not in have:
                con.execute(f'alter table screens add column "{c}"')
        _clean(df).to_sql("screens", con, if_exists="append", index=False)


def save_history(xs: pd.DataFrame) -> None:
    """The point-in-time cross-sections behind the backtest (replaced on rebuild)."""
    with sqlite3.connect(LOCAL_SQLITE) as con:
        _clean(xs).to_sql("history", con, if_exists="replace", index=False)


def load_history() -> pd.DataFrame | None:
    if not LOCAL_SQLITE.exists():
        return None
    with sqlite3.connect(LOCAL_SQLITE) as con:
        try:
            df = pd.read_sql("select * from history", con, parse_dates=["date", "period_end", "bs_date"])
        except Exception:
            return None
    return df if len(df) else None

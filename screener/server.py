"""QUAINT web app: `python3 -m screener app`, then open http://localhost:8765.

Serves web/ plus a JSON API. With DATABASE_URL set, everything comes from the Postgres
warehouse (latest screen run, filings, prices, insider trades, paper-portfolio scorecard);
without it, from the static export in web/data.
  api/ping              -> {"ok": true, "db": bool}
  api/index             -> every company's latest screen numbers (search, screen, compare)
  api/company/<T>       -> history + screen row for any SEC-registered ticker
  api/news/<T>          -> recent headlines (Google News RSS)
  api/scorecard         -> forward record of the monthly top 25 vs SPY / IJR / IJH
  api/runs              -> stored screen runs
Set QUAINT_PASSWORD to require a password (any username) when it's reachable from the internet.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import webbrowser
from pathlib import Path

import pandas as pd

from . import appdata, config, db

WEB = appdata.OUT.parent
CACHE = config.DATA_DIR / "app"
SKELETON = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>')
PASSWORD = os.environ.get("QUAINT_PASSWORD", "")
TICKER = r"^[A-Za-z0-9.\-]{1,10}$"
_lock = threading.Lock()
_mem: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _mem[key] = (time.time(), v)
    return v


def index() -> dict:
    if db.enabled():
        from . import warehouse
        idx = _cached("index", 300, warehouse.latest_index)
        if idx:
            return idx
    p = appdata.OUT / "index.json"
    if not p.exists():
        raise LookupError("No screen yet: run `python3 -m screener screen` first.")
    return _cached("index-file", 300, lambda: json.loads(p.read_text()))


def _row(t: str) -> dict | None:
    return next((r for r in index()["rows"] if r["t"] == t), None)


def company(t: str) -> dict:
    """Universe names come straight from the warehouse (or the export); anything else is
    pulled from SEC/Yahoo, scored against today's screen, and stored for next time."""
    return _cached(f"co:{t}", 3600, lambda: _company(t))


def _company(t: str) -> dict:
    row = _row(t)
    if db.enabled() and row and row.get("cik"):
        from . import warehouse
        cik = int(row["cik"])
        cf = warehouse.load_cf(cik)
        if cf is None:
            warehouse.sync_company(cik)
            cf = warehouse.load_cf(cik)
        c = appdata.company(t, cf, warehouse.load_prices([t]), warehouse.load_trades([cik]), cik)
        c["row"] = row
        return c
    if not db.enabled() and row:
        chunk = appdata.OUT / f"c_{appdata._chunk(t)}.json"
        if chunk.exists():
            c = json.loads(chunk.read_text()).get(t)
            if c:
                return c
    p = CACHE / f"{t}.json"
    if p.exists() and time.time() - p.stat().st_mtime < 86400:
        return json.loads(p.read_text())
    from . import edgar, insider, lookup, prices, score, screen, universe
    with _lock:  # one external lookup at a time: SEC rate limits, shared caches
        latest = pd.read_pickle(screen.LATEST)
        try:
            ext = lookup._external_row(t, latest)
        except SystemExit as e:
            raise LookupError(str(e))
        s = score.score(pd.concat([latest, ext], ignore_index=True).set_index("ticker"))
        uni = universe.load().set_index("ticker")
        r = next(x for x in appdata.index_rows(s, uni) if x["t"] == t)
        sec = universe.sec_ticker_map().drop_duplicates("ticker").set_index("ticker")
        cik = int(sec.at[t, "cik"])
        r["cik"] = cik
        px = prices.load([t], name=f"single_{t}_")
        if db.enabled():
            from . import warehouse
            warehouse.sync_company(cik)
            cf, trades = warehouse.load_cf(cik), warehouse.load_trades([cik])
        else:
            cf, trades = edgar.fetch(cik), (insider.build() if insider.TRADES.exists() else None)
        c = appdata.company(t, cf, px, trades, cik)
        c["row"] = r
    CACHE.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(c, separators=(",", ":")))
    return c


def news(t: str) -> list:
    from . import events
    r = _row(t)
    return _cached(f"news:{t}", 1800,
                   lambda: appdata.news_rows({t: events.headlines(r["n"] if r else t, t, n=6)}).get(t, []))


def create_app():
    from fastapi import FastAPI, HTTPException, Path as P, Request
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="QUAINT", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def password(request: Request, call_next):
        if PASSWORD:
            h = request.headers.get("authorization", "")
            ok = False
            if h.startswith("Basic "):
                try:
                    ok = secrets.compare_digest(base64.b64decode(h[6:]).decode().split(":", 1)[1], PASSWORD)
                except Exception:
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="QUAINT"'})
        return await call_next(request)

    def guard(fn, *a):
        try:
            return fn(*a)
        except LookupError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/ping")
    def ping():
        return {"ok": True, "db": db.enabled()}

    @app.get("/api/index")
    def api_index():
        return guard(index)

    @app.get("/api/company/{t}")
    def api_company(t: str = P(pattern=TICKER)):
        return guard(company, t.upper())

    @app.get("/api/news/{t}")
    def api_news(t: str = P(pattern=TICKER)):
        return guard(news, t.upper())

    @app.get("/api/scorecard")
    def api_scorecard():
        if not db.enabled():
            raise HTTPException(404, "The scorecard needs the database (DATABASE_URL).")
        from . import portfolio
        return _cached("scorecard", 600, portfolio.scorecard)

    @app.get("/api/runs")
    def api_runs():
        if not db.enabled():
            return []
        df = db.read("select id, run_at, as_of, method, n from screen_runs order by id desc limit 100")
        return JSONResponse(json.loads(df.to_json(orient="records", date_format="iso")))

    @app.get("/explorer.html", response_class=HTMLResponse)
    def explorer_page():
        return explorer_html()

    @app.get("/", response_class=HTMLResponse)
    def home():
        return SKELETON + (WEB / "index.html").read_text() + "</body></html>"

    app.mount("/", StaticFiles(directory=str(WEB)), name="web")
    return app


def serve(port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    import uvicorn
    url = f"http://localhost:{port}/"
    print(f"QUAINT running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


def explorer_html() -> str:
    """The backtest explorer. The refresh writes it; if it is missing or older than the last
    screen (e.g. the app started before any refresh wrote one), build it here from that screen."""
    from . import explorer, score, screen
    with _lock:
        live = explorer.LIVE
        if screen.LATEST.exists() and (not live.exists() or live.stat().st_mtime < screen.LATEST.stat().st_mtime):
            s = score.score(pd.read_pickle(screen.LATEST).set_index("ticker"))
            explorer.write(s, config.DATA_DIR / "reports")
        if live.exists():
            return live.read_text()
    if (WEB / "explorer.html").exists():
        return (WEB / "explorer.html").read_text()
    return (SKELETON + '<p style="font:16px system-ui;margin:40px">The backtest explorer appears after the first '
            "refresh finishes. Check progress with <code>./quaint logs</code>.</p></body></html>")


def build_from_latest(fetch_news: bool = True) -> Path:
    """Rebuild web/data from the last live screen without fetching prices again."""
    from . import events, score, screen
    s = score.score(pd.read_pickle(screen.LATEST).set_index("ticker"))
    news = {}
    if fetch_news:
        core = s[s["passes"] & ~s["is_financial"].astype(bool)]
        news = {t: events.headlines(str(s.at[t, "sec_name"]), t) for t in score.top_n(core, "composite", 25).index}
    return appdata.build(s, news)

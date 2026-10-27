"""Local server for the research app: `python3 -m screener app`, then open http://localhost:8765.

Serves web/ (the same files the hosted page uses) plus a small API:
  api/ping            -> {"ok": true}; tells the page it can look tickers up live
  api/company/<T>     -> history + screen row for any SEC-registered ticker (cached a day)
  api/news/<T>        -> recent headlines (Google News RSS)
"""
from __future__ import annotations

import json
import re
import threading
import time
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

from . import appdata, config

WEB = appdata.OUT.parent
CACHE = config.DATA_DIR / "app"
SKELETON = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>')
_lock = threading.Lock()


def _company(t: str) -> dict | None:
    """Universe names come from the exported chunks; anything else is pulled and scored on demand."""
    p = CACHE / f"{t}.json"
    if p.exists() and time.time() - p.stat().st_mtime < 86400:
        return json.loads(p.read_text())
    chunk = appdata.OUT / f"c_{appdata._chunk(t)}.json"
    if chunk.exists():
        c = json.loads(chunk.read_text()).get(t)
        if c:
            return c
    from . import edgar, insider, lookup, prices, score, screen, universe
    with _lock:  # one external lookup at a time: SEC rate limits, shared caches
        latest = pd.read_pickle(screen.LATEST)
        try:
            row = lookup._external_row(t, latest)
        except SystemExit as e:
            return {"t": t, "error": str(e)}
        s = score.score(pd.concat([latest, row], ignore_index=True).set_index("ticker"))
        uni = universe.load().set_index("ticker")
        r = next(x for x in appdata.index_rows(s, uni) if x["t"] == t)
        sec = universe.sec_ticker_map().drop_duplicates("ticker").set_index("ticker")
        cik = int(sec.at[t, "cik"])
        r["cik"] = cik
        px = prices.load([t], name=f"single_{t}_")
        trades = insider.build() if insider.TRADES.exists() else None
        c = appdata.company(t, edgar.fetch(cik), px, trades, cik)
        c["row"] = r
    CACHE.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(c, separators=(",", ":")))
    return c


def _news(t: str) -> list:
    from . import events
    idx = json.loads((appdata.OUT / "index.json").read_text())
    name = next((r["n"] for r in idx["rows"] if r["t"] == t), t)
    return [{"title": h["title"], "src": h["source"], "link": h["link"],
             "date": f"{h['date']:%b %d}" if pd.notna(h["date"]) else ""} for h in events.headlines(name, t, n=6)]


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        m = re.fullmatch(r"/api/(ping|company|news)(?:/([A-Za-z0-9.\-]{1,10}))?", path)
        if m:
            kind, t = m.group(1), (m.group(2) or "").upper()
            try:
                if kind == "ping":
                    return self._json({"ok": True})
                if kind == "company" and t:
                    c = _company(t)
                    return self._json(c) if c and "error" not in c else self._json(c or {"error": "not found"}, 404)
                if kind == "news" and t:
                    return self._json(_news(t))
            except Exception as e:  # report, don't crash the server
                return self._json({"error": str(e)}, 500)
            return self._json({"error": "bad request"}, 400)
        if path in ("/", "/index.html"):
            b = (SKELETON + (WEB / "index.html").read_text() + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        return super().do_GET()


def serve(port: int = 8765, open_browser: bool = True) -> None:
    if not (appdata.OUT / "index.json").exists():
        raise SystemExit("No app data yet: run `python3 -m screener screen` (or `app --build`) first.")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(WEB)))
    url = f"http://localhost:{port}/"
    print(f"QUAINT running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def build_from_latest(fetch_news: bool = True) -> Path:
    """Rebuild web/data from the last live screen without fetching prices again."""
    from . import events, score, screen
    s = score.score(pd.read_pickle(screen.LATEST).set_index("ticker"))
    news = {}
    if fetch_news:
        core = s[s["passes"] & ~s["is_financial"].astype(bool)]
        news = {t: events.headlines(str(s.at[t, "sec_name"]), t) for t in score.top_n(core, "composite", 25).index}
    return appdata.build(s, news)

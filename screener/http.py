"""Thin requests wrapper: shared session, retries with backoff, per-host rate limit."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import requests

_session = requests.Session()
_lock = threading.Lock()
_last_call: dict[str, float] = {}


def get(url: str, *, headers: dict | None = None, max_rps: float = 5.0,
        retries: int = 4, timeout: float = 30.0) -> requests.Response:
    host = urlparse(url).netloc
    delay = 2.0
    for attempt in range(retries + 1):
        with _lock:
            wait = _last_call.get(host, 0.0) + 1.0 / max_rps - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call[host] = time.monotonic()
        try:
            r = _session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException:
            if attempt == retries:
                raise
        else:
            if r.status_code < 400 or r.status_code == 404:
                return r
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == retries:
                r.raise_for_status()
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")

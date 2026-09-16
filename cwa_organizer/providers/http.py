"""Small JSON HTTP client: per-host rate limiting, retries with backoff, on-disk cache."""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "cwa-organizer/1.0 (+personal Calibre library organizer)"


class HttpError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class JsonCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.execute("CREATE TABLE IF NOT EXISTS http_cache (k TEXT PRIMARY KEY, ts REAL, body TEXT)")
        self.con.commit()

    def get(self, k: str, max_age_days: float):
        row = self.con.execute("SELECT ts, body FROM http_cache WHERE k=?", (k,)).fetchone()
        if row and time.time() - row[0] < max_age_days * 86400:
            return json.loads(row[1])
        return None

    def put(self, k: str, value) -> None:
        self.con.execute("INSERT OR REPLACE INTO http_cache VALUES (?,?,?)", (k, time.time(), json.dumps(value)))
        self.con.commit()

    def close(self):
        self.con.close()


class JsonClient:
    def __init__(self, cache: JsonCache, min_interval: float, cache_days: float, log=None,
                 max_retries: int = 4, sleep=time.sleep):
        self.cache = cache
        self.min_interval = min_interval
        self.cache_days = cache_days
        self.log = log or (lambda *a, **k: None)
        self.max_retries = max_retries
        self._last = 0.0
        self._sleep = sleep
        self.requests_made = 0

    def _wait_turn(self):
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            self._sleep(self.min_interval - gap)
        self._last = time.monotonic()

    def request(self, method: str, url: str, body=None, headers=None, cache_key_extra: str = "", use_cache=True):
        payload = json.dumps(body).encode() if body is not None else None
        k = hashlib.sha256(f"{method} {url} {cache_key_extra} ".encode() + (payload or b"")).hexdigest()
        if use_cache:
            hit = self.cache.get(k, self.cache_days)
            if hit is not None:
                return hit
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if payload is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        attempt = 0
        while True:
            attempt += 1
            self._wait_turn()
            req = urllib.request.Request(url, data=payload, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self.requests_made += 1
                if use_cache:
                    self.cache.put(k, data)
                return data
            except urllib.error.HTTPError as e:
                status = e.code
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if status in (429, 500, 502, 503, 504) and attempt <= self.max_retries:
                    wait = _backoff(attempt, retry_after)
                    self.log(f"    {url}: HTTP {status}, retrying in {wait:.0f}s")
                    self._sleep(wait)
                    continue
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                raise HttpError(f"HTTP {status} from {url}: {detail}", status) from e
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                if attempt <= self.max_retries:
                    wait = _backoff(attempt, None)
                    self.log(f"    {url}: {e}, retrying in {wait:.0f}s")
                    self._sleep(wait)
                    continue
                raise HttpError(f"{url}: {e}") from e


def _backoff(attempt: int, retry_after) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 600.0)
        except ValueError:
            pass
    return min(5 * 2 ** (attempt - 1), 120) + random.uniform(0, 2)

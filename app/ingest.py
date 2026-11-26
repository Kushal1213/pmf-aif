"""Loads scheme list + full NAV history from mfapi.in (a public mirror of AMFI data).

Robustness features: global rate limiter, retry with exponential backoff + jitter, Retry-After
support (429 also pauses *all* workers), garbled-JSON retry, row-level validation, isolated-spike
removal, idempotent upserts, resumable runs (skips schemes loaded recently; failures are retried
on the next run).

    python -m app.ingest load [--limit N] [--concurrency 8] [--rps 5] [--strict] [--with-metrics]
    python -m app.ingest metrics
    python -m app.ingest demo           # synthetic data, for offline use
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
import numpy as np
import pandas as pd
from sqlalchemy import func, select

from .db import (ACTIVE_WINDOW_DAYS, METRIC_COLUMNS, get_engine, load_series, metadata, scheme_metrics,
                 schemes, navs, upsert)
from .metrics import DEFAULT_RF, summary_metrics

log = logging.getLogger("ingest")
BASE_URL = os.getenv("MFAPI_BASE_URL", "https://api.mfapi.in")


# ---------------------------------------------------------------- cleaning
def drop_isolated_spikes(rows, thresh: float = 0.5):
    """Remove one-off points (e.g. decimal-shift typos) that jump >thresh away from both
    neighbours while the neighbours agree with each other (within 20%)."""
    keep = [True] * len(rows)
    for i in range(1, len(rows) - 1):
        p, c, n = rows[i - 1][1], rows[i][1], rows[i + 1][1]
        if abs(p / n - 1) < 0.2 and abs(c / p - 1) > thresh and abs(c / n - 1) > thresh:
            keep[i] = False
    return [r for r, k in zip(rows, keep) if k], keep.count(False)


def clean_nav_rows(raw) -> tuple[list[tuple[date, float]], int]:
    """raw: [{'date': 'dd-mm-yyyy', 'nav': '12.34'}, ...] -> (sorted [(date, nav)], rejected_count)."""
    good: dict[date, float] = {}
    rejected = 0
    limit = date.today() + timedelta(days=1)
    for item in raw or []:
        try:
            d = datetime.strptime(item["date"], "%d-%m-%Y").date()
            v = float(item["nav"])
        except (KeyError, TypeError, ValueError):
            rejected += 1
            continue
        if not math.isfinite(v) or v <= 0 or d > limit:
            rejected += 1
            continue
        if d in good:
            rejected += 1          # duplicate date: last one wins
        good[d] = v
    rows, spikes = drop_isolated_spikes(sorted(good.items()))
    return rows, rejected + spikes


# ---------------------------------------------------------------- HTTP
class SourceError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, rps: float = 5.0, max_retries: int = 6, base_delay: float = 1.0):
        if not math.isfinite(rps) or rps <= 0:
            raise ValueError("rps must be a positive finite number")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if not math.isfinite(base_delay) or base_delay < 0:
            raise ValueError("base_delay must be a non-negative finite number")
        self.client, self.interval = client, 1.0 / rps
        self.max_retries, self.base_delay = max_retries, base_delay
        self._next, self._lock = 0.0, asyncio.Lock()

    async def _throttle(self):
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if wait:
            await asyncio.sleep(wait)

    def _backoff(self, attempt: int) -> float:
        return min(60.0, self.base_delay * 2 ** attempt) * (0.5 + random.random())

    @staticmethod
    def _retry_after(value: str) -> float | None:
        """Parse either Retry-After delta-seconds or its HTTP-date form."""
        value = value.strip()
        if not value:
            return None
        try:
            return max(0.0, min(300.0, float(value)))
        except ValueError:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, min(300.0, (dt - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                return None

    async def get_json(self, path: str):
        """JSON body, or None on 404. Raises SourceError after exhausting retries."""
        for attempt in range(self.max_retries + 1):
            await self._throttle()
            try:
                r = await self.client.get(path, timeout=30)
            except httpx.TransportError:
                delay = self._backoff(attempt)
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        delay = self._backoff(attempt)      # truncated / HTML error page
                elif r.status_code == 404:
                    return None
                elif r.status_code == 429 or r.status_code >= 500:
                    ra = self._retry_after(r.headers.get("Retry-After", ""))
                    delay = ra if ra is not None else self._backoff(attempt)
                    if r.status_code == 429:   # slow every worker down, not just this one
                        async with self._lock:
                            self._next = max(self._next, time.monotonic() + delay)
                else:
                    r.raise_for_status()
            if attempt < self.max_retries:
                await asyncio.sleep(delay)
        raise SourceError(f"giving up on {path}")


# ---------------------------------------------------------------- storage
def store_scheme(engine, code: int, name: str, meta: dict, rows, rejected: int):
    meta = meta or {}
    with engine.begin() as conn:
        for i in range(0, len(rows), 5000):
            upsert(conn, navs, [{"scheme_code": code, "date": d, "nav": v} for d, v in rows[i:i + 5000]],
                   ["scheme_code", "date"], ["nav"])
        rec = {"scheme_code": code, "name": meta.get("scheme_name") or name,
               "fund_house": meta.get("fund_house"), "scheme_type": meta.get("scheme_type"),
               "category": meta.get("scheme_category"),
               "isin": meta.get("isin_growth") or meta.get("isin_div_reinvestment"),
               "first_nav_date": rows[0][0] if rows else None, "last_nav_date": rows[-1][0] if rows else None,
               "nav_count": len(rows), "rejected_rows": rejected, "loaded_at": datetime.now(timezone.utc).replace(tzinfo=None)}
        upsert(conn, schemes, [rec], ["scheme_code"], [k for k in rec if k != "scheme_code"])


async def load_all(engine, limit=None, concurrency=8, rps=5.0, max_age_hours=20,
                   base_url=BASE_URL, transport=None, **fetch_kw) -> dict:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a non-negative integer")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if not math.isfinite(max_age_hours) or max_age_hours < 0:
        raise ValueError("max_age_hours must be a non-negative finite number")
    metadata.create_all(engine)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=max_age_hours)
    with engine.connect() as c:
        fresh = {r[0] for r in c.execute(select(schemes.c.scheme_code).where(schemes.c.loaded_at >= cutoff))}
    stats = {"ok": 0, "skipped": 0, "failed": [], "rows": 0, "rejected": 0}
    async with httpx.AsyncClient(base_url=base_url, transport=transport,
                                 headers={"User-Agent": "mf-analytics-api/1.0"}) as client:
        f = Fetcher(client, rps, **fetch_kw)
        listing = await f.get_json("/mf")
        if not isinstance(listing, list):
            raise SourceError("scheme-list response must be a JSON array")
        found, seen = [], set()
        for x in listing:
            if not isinstance(x, dict) or not str(x.get("schemeCode", "")).isdigit():
                continue
            code = int(x["schemeCode"])
            if code not in seen:
                seen.add(code)
                found.append((code, x.get("schemeName") or str(code)))
        todo = found
        if listing and not todo:
            raise SourceError("scheme-list response contained no valid scheme codes")
        stats["skipped"] = sum(1 for c, _ in todo if c in fresh)
        todo = [t for t in todo if t[0] not in fresh][:limit]
        q: asyncio.Queue = asyncio.Queue()
        for t in todo:
            q.put_nowait(t)
        write_lock = asyncio.Lock()

        async def worker():
            while True:
                try:
                    code, name = q.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    payload = await f.get_json(f"/mf/{code}")
                    if payload is None:  # scheme disappeared between list and history fetch
                        payload = {"meta": {"scheme_name": name}, "data": []}
                    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict) \
                            or not isinstance(payload.get("data"), list):
                        raise SourceError(f"invalid history response for scheme {code}: expected meta object and data array")
                    rows, rej = clean_nav_rows(payload["data"])
                    if payload["data"] and not rows:
                        raise SourceError(f"all NAV rows were rejected for scheme {code}; leaving it eligible for retry")
                    async with write_lock:       # serialise writers (SQLite); harmless on Postgres
                        await asyncio.to_thread(store_scheme, engine, code, name, payload.get("meta"), rows, rej)
                    stats["ok"] += 1; stats["rows"] += len(rows); stats["rejected"] += rej
                    if stats["ok"] % 200 == 0:
                        log.info("loaded %d/%d schemes", stats["ok"], len(todo))
                except Exception as e:  # noqa: BLE001 - one bad scheme must not kill the run
                    log.warning("scheme %s failed: %s", code, e)
                    stats["failed"].append(code)

        await asyncio.gather(*[worker() for _ in range(concurrency)])
    return stats


def compute_metrics(engine, rf: float = DEFAULT_RF, batch: int = 500) -> int:
    """Refresh scheme_metrics for active schemes. A scheme that fails is logged and skipped."""
    metadata.create_all(engine)
    with engine.connect() as conn:
        ref = conn.scalar(select(func.max(schemes.c.last_nav_date)))
        if ref is None:
            return 0
        active = select(schemes.c.scheme_code).where(
            schemes.c.last_nav_date >= ref - timedelta(days=ACTIVE_WINDOW_DAYS), schemes.c.nav_count > 1)
        codes = [r[0] for r in conn.execute(active)]
        with engine.begin() as w:       # drop metrics of schemes that are no longer active
            w.execute(scheme_metrics.delete().where(scheme_metrics.c.scheme_code.not_in(active)))
        buf, n, failed = [], 0, 0

        def flush():
            nonlocal buf, n
            if buf:
                with engine.begin() as w:
                    upsert(w, scheme_metrics, buf, ["scheme_code"], ["as_of", *METRIC_COLUMNS])
                n += len(buf)
                buf = []

        for code in codes:
            try:
                s = load_series(conn, code)
                if s.empty:
                    continue
                buf.append({"scheme_code": code, "as_of": s.index[-1].date(), **summary_metrics(s, rf)})
            except Exception as e:  # noqa: BLE001 - one odd series must not stop the batch
                failed += 1
                log.warning("metrics for scheme %s failed: %s", code, e)
            if len(buf) >= batch:
                flush()
        flush()
    if failed:
        log.warning("metrics failed for %d schemes", failed)
    return n


def seed_demo(engine, n: int = 30, years: int = 11, seed: int = 7):
    """Synthetic GBM NAVs (with ~2% random missing days) for offline demos/tests."""
    metadata.create_all(engine)
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(end=pd.Timestamp(date.today()), periods=int(years * 252))
    cats = ["Equity Scheme - Large Cap Fund", "Equity Scheme - Flexi Cap Fund", "Debt Scheme - Liquid Fund"]
    for i in range(n):
        cat = cats[i % 3]
        sig = (0.005 if "Debt" in cat else rng.uniform(.10, .22)) / math.sqrt(252)
        mu = (0.07 if "Debt" in cat else rng.uniform(.08, .16)) / 252
        nav = 10 * np.exp(np.cumsum(rng.normal(mu, sig, len(days))))
        keep = rng.random(len(days)) > 0.02
        rows = list(zip([d.date() for d in days[keep]], nav[keep].round(4)))
        store_scheme(engine, 100000 + i, f"Demo Fund {i}",
                     {"fund_house": f"House {i % 4}", "scheme_type": "Open Ended Schemes", "scheme_category": cat}, rows, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("load", help="fetch scheme list + NAV history (resumable)")
    l.add_argument("--limit", type=int, help="max schemes to fetch this run (smoke tests)")
    l.add_argument("--concurrency", type=int, default=8)
    l.add_argument("--rps", type=float, default=5.0, help="global requests per second")
    l.add_argument("--max-age-hours", type=float, default=20, help="skip schemes loaded more recently than this")
    l.add_argument("--strict", action="store_true", help="exit with status 1 if any scheme failed")
    l.add_argument("--with-metrics", action="store_true", help="refresh scheme_metrics after loading")
    m = sub.add_parser("metrics", help="recompute screening metrics")
    m.add_argument("--rf", type=float, default=DEFAULT_RF)
    d = sub.add_parser("demo", help="seed synthetic data for offline use")
    d.add_argument("--n", type=int, default=30)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per request is too noisy at 40k schemes
    eng = get_engine()
    if a.cmd == "load":
        try:
            st = asyncio.run(load_all(eng, a.limit, a.concurrency, a.rps, a.max_age_hours))
        except (ValueError, SourceError) as e:
            log.error("load aborted: %s", e)
            return 1
        log.info("done: ok=%d skipped=%d failed=%d rows=%d rejected=%d", st["ok"], st["skipped"],
                 len(st["failed"]), st["rows"], st["rejected"])
        if st["failed"]:
            shown = ", ".join(map(str, sorted(st["failed"])[:20]))
            log.warning("failed schemes (re-run to retry): %s%s", shown, " ..." if len(st["failed"]) > 20 else "")
        if a.with_metrics:
            log.info("metrics for %d schemes", compute_metrics(eng))
        return 1 if a.strict and st["failed"] else 0
    if a.cmd == "metrics":
        log.info("metrics for %d schemes", compute_metrics(eng, a.rf))
        return 0
    seed_demo(eng, a.n)
    log.info("demo seeded; now run: python -m app.ingest metrics")
    return 0


if __name__ == "__main__":
    sys.exit(main())

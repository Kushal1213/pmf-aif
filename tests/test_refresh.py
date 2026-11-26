"""Incremental refresh, dead-scheme handling and snapshot round trip."""
import asyncio
import sqlite3
from datetime import date, timedelta

import httpx
from sqlalchemy import select

from app import snapshot as snap
from app.db import get_engine, navs, schemes
from app.ingest import load_all, refresh_all, seed_demo

D = lambda d: d.strftime("%d-%m-%Y")  # noqa: E731 - mfapi date format


def run(coro):
    return asyncio.run(coro)


def test_dead_scheme_with_all_zero_navs_is_stored_not_retried(tmp_path):
    def handler(req):
        if req.url.path == "/mf":
            return httpx.Response(200, json=[{"schemeCode": 5, "schemeName": "Dead"}])
        return httpx.Response(200, json={"meta": {"scheme_name": "Dead fund"},
                                         "data": [{"date": "05-07-2006", "nav": "0.00000"}] * 3})
    eng = get_engine(f"sqlite:///{tmp_path / 'd.db'}")
    st = run(load_all(eng, transport=httpx.MockTransport(handler), rps=1000, max_retries=0))
    assert st["ok"] == 1 and st["failed"] == []
    with eng.connect() as c:
        row = c.execute(select(schemes).where(schemes.c.scheme_code == 5)).mappings().one()
    assert row["nav_count"] == 0 and row["rejected_rows"] == 3 and row["last_nav_date"] is None
    assert run(load_all(eng, transport=httpx.MockTransport(handler), rps=1000))["skipped"] == 1   # not retried


def test_refresh_is_incremental(tmp_path):
    today = date.today()
    old_last = today - timedelta(days=3)
    seen_paths = []

    def handler(req):
        seen_paths.append(str(req.url.raw_path, "ascii"))
        p = req.url.path
        if p == "/mf":
            return httpx.Response(200, json=[{"schemeCode": c, "schemeName": f"S{c}"} for c in (1, 2, 3)])
        if p == "/mf/1":   # active: only the recent tail is requested and returned
            start = date.fromisoformat(req.url.params["startDate"])
            assert start == old_last - timedelta(days=7)
            return httpx.Response(200, json={"meta": {"scheme_name": "S1", "scheme_category": "Equity"},
                                             "data": [{"date": D(today), "nav": "12.5"}, {"date": D(old_last), "nav": "12.0"}]})
        if p == "/mf/3":   # new scheme: full history
            assert "startDate" not in req.url.params
            return httpx.Response(200, json={"meta": {"scheme_name": "S3"},
                                             "data": [{"date": D(today - timedelta(days=i)), "nav": str(10 + i)} for i in range(5)]})
        raise AssertionError(f"inactive scheme fetched: {p}")

    eng = get_engine(f"sqlite:///{tmp_path / 'r.db'}")
    seed = [(old_last - timedelta(days=i), 12.0 - i * 0.01) for i in range(200, -1, -1)]
    from app.db import metadata
    from app.ingest import store_scheme
    metadata.create_all(eng)
    store_scheme(eng, 1, "S1", {"scheme_category": "Equity"}, seed, 2)
    store_scheme(eng, 2, "S2", {}, [(today - timedelta(days=400), 5.0), (today - timedelta(days=399), 5.1)], 0)

    st = run(refresh_all(eng, transport=httpx.MockTransport(handler), rps=1000))
    assert (st["new"], st["updated"], st["skipped"], st["ok"], st["failed"]) == (1, 1, 1, 2, [])
    with eng.connect() as c:
        s1 = c.execute(select(schemes).where(schemes.c.scheme_code == 1)).mappings().one()
        assert s1["last_nav_date"] == today and s1["first_nav_date"] == seed[0][0]   # history kept, tail added
        assert s1["nav_count"] == len(seed) + 1 and s1["rejected_rows"] == 2
        assert c.scalar(select(navs.c.nav).where(navs.c.scheme_code == 1, navs.c.date == today)) == 12.5
        assert c.execute(select(schemes.c.nav_count).where(schemes.c.scheme_code == 3)).scalar() == 5
    assert not any(p.startswith("/mf/2") for p in seen_paths)


def test_snapshot_round_trip(tmp_path):
    db = tmp_path / "live.db"
    eng = get_engine(f"sqlite:///{db}")
    seed_demo(eng, n=3, years=1)
    gz = snap.make_snapshot(db, tmp_path / "out" / "mf.db.gz")
    assert gz.stat().st_size > 0          # includes rows still in the WAL (VACUUM INTO reads them)
    restored = snap.restore_file(gz, tmp_path / "restored" / "mf.db")
    con = sqlite3.connect(restored)
    try:
        assert con.execute("select count(*) from schemes").fetchone()[0] == 3
        assert con.execute("pragma integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    assert snap.sqlite_path("sqlite:///x/y.db").as_posix() == "x/y.db"


def test_hosted_export_trims_and_api_reports_it(tmp_path):
    from fastapi.testclient import TestClient
    from app.db import metadata
    from app.ingest import compute_metrics, store_scheme
    from app.main import create_app

    full = tmp_path / "full.db"
    eng = get_engine(f"sqlite:///{full}")
    metadata.create_all(eng)
    today = date.today()
    days = [today - timedelta(days=i) for i in range(9 * 365, -1, -1)]                  # 9 years, active
    store_scheme(eng, 1, "Active Fund - Direct Plan - Growth", {"scheme_category": "Equity"},
                 [(d, 10 * 1.0003 ** i) for i, d in enumerate(days)], 0)
    old = [today - timedelta(days=i) for i in range(3000, 2000, -1)]                     # closed years ago
    store_scheme(eng, 2, "Closed Fund", {"scheme_category": "Equity"}, [(d, 10.0 + i / 1000) for i, d in enumerate(old)], 0)
    compute_metrics(eng)

    hosted = tmp_path / "hosted.db"
    st = snap.export_hosted(full, hosted, years=5, active_days=30, margin_days=15)
    assert st["schemes"] == 2 and st["schemes_with_history"] == 1
    con = sqlite3.connect(hosted)
    try:
        first_kept = date.fromisoformat(con.execute("select min(date) from navs").fetchone()[0])
        assert (today - first_kept).days <= 5 * 366 + 15
        # metadata keeps the true inception date and count
        assert con.execute("select first_nav_date, nav_count from schemes where scheme_code=1").fetchone() == (days[0].isoformat(), len(days))
    finally:
        con.close()

    heng = get_engine(f"sqlite:///{hosted}")
    store_scheme(heng, 1, "Active Fund - Direct Plan - Growth", {"scheme_category": "Equity"},
                 [(today, 99.0)], 0, partial=True)                                         # a refresh on trimmed data
    with heng.connect() as c:
        first, count = c.execute(select(schemes.c.first_nav_date, schemes.c.nav_count).where(schemes.c.scheme_code == 1)).one()
    assert first == days[0] and count == len(days)                                         # inception + true count kept

    with TestClient(create_app(heng, rate_limit_per_min=0)) as cl:
        r = cl.get("/schemes/1/returns").json()
        assert r["history_from"] == first_kept.isoformat()
        assert r["returns"]["since_inception"] is None and r["returns"]["5y"] is not None and r["returns"]["10y"] is None
        assert cl.get("/schemes/1/analytics").json()["history_from"] == first_kept.isoformat()
        gone = cl.get("/schemes/2/returns")
        assert gone.status_code == 404 and "inactive" in gone.json()["detail"]
        assert cl.get("/schemes", params={"q": "closed"}).json()["total"] == 1        # still searchable

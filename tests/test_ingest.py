import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from app.db import get_engine, navs, schemes
from app.ingest import Fetcher, clean_nav_rows, load_all
from sqlalchemy import func, select


def test_clean_rows_rejects_bad_data():
    raw = [{"date": "01-01-2024", "nav": "10.0"}, {"date": "02-01-2024", "nav": "N.A."},
           {"date": "03-01-2024", "nav": "-5"}, {"date": "bad", "nav": "1"}, {"date": "04-01-2024", "nav": "0"},
           {"date": "05-01-2024", "nav": "10.2"}, {"date": "05-01-2024", "nav": "10.3"}, {"nav": "1"},
           {"date": "01-01-2999", "nav": "10"}, {"date": "06-01-2024", "nav": "inf"}]
    rows, rej = clean_nav_rows(raw)
    assert [(str(d), v) for d, v in rows] == [("2024-01-01", 10.0), ("2024-01-05", 10.3)]
    assert rej == 8


def test_isolated_spike_removed_but_real_move_kept():
    mk = lambda vals: [{"date": f"{i+1:02d}-01-2024", "nav": str(v)} for i, v in enumerate(vals)]
    rows, rej = clean_nav_rows(mk([10, 10.1, 101, 10.2, 10.3]))        # decimal-shift typo
    assert rej == 1 and 101 not in [v for _, v in rows]
    rows, rej = clean_nav_rows(mk([10, 10.1, 20, 20.1, 20.2]))         # genuine step (e.g. NFO/merger)
    assert rej == 0 and len(rows) == 5


def run(coro):
    return asyncio.run(coro)


def test_fetcher_retries_429_500_and_garbage():
    calls = []
    seq = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(500),
           httpx.Response(200, content=b"<html>oops"), httpx.Response(200, json={"ok": 1})]

    def handler(req):
        calls.append(1)
        return seq[len(calls) - 1]

    async def go():
        async with httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler)) as c:
            return await Fetcher(c, rps=1000, base_delay=0.001).get_json("/a")
    assert run(go()) == {"ok": 1} and len(calls) == 4


def test_fetcher_gives_up_and_404_is_none():
    async def go(status):
        async with httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(lambda r: httpx.Response(status))) as c:
            return await Fetcher(c, rps=1000, max_retries=2, base_delay=0.001).get_json("/a")
    assert run(go(404)) is None
    try:
        run(go(503)); assert False
    except RuntimeError:
        pass


def test_fetcher_retry_after_http_date_and_bad_configuration():
    future = datetime.now(timezone.utc) + timedelta(seconds=20)
    parsed = Fetcher._retry_after(format_datetime(future))
    assert parsed is not None and 0 < parsed <= 20
    assert Fetcher._retry_after("nonsense") is None
    async def invalid():
        async with httpx.AsyncClient() as c:
            Fetcher(c, rps=0)
    try:
        run(invalid()); assert False
    except ValueError as exc:
        assert "rps" in str(exc)


def test_load_all_end_to_end_idempotent_and_isolates_failures(tmp_path):
    def handler(req):
        p = req.url.path
        if p == "/mf":
            return httpx.Response(200, json=[{"schemeCode": 1, "schemeName": "A"}, {"schemeCode": 2, "schemeName": "B"}])
        if p == "/mf/1":
            return httpx.Response(200, json={"meta": {"scheme_category": "Equity", "fund_house": "H"},
                                             "data": [{"date": "02-01-2024", "nav": "11"}, {"date": "01-01-2024", "nav": "10"}, {"date": "03-01-2024", "nav": "x"}]})
        return httpx.Response(500)
    eng = get_engine(f"sqlite:///{tmp_path/'t.db'}")
    kw = dict(transport=httpx.MockTransport(handler), rps=1000, max_retries=1, base_delay=0.001, concurrency=2)
    st = run(load_all(eng, **kw))
    assert st["ok"] == 1 and st["failed"] == [2] and st["rejected"] == 1
    st2 = run(load_all(eng, **kw))                      # resumes: scheme 1 skipped, 2 retried
    assert st2["skipped"] == 1
    with eng.connect() as c:
        assert c.scalar(select(func.count()).select_from(navs)) == 2
        assert c.scalar(select(schemes.c.category).where(schemes.c.scheme_code == 1)) == "Equity"


def test_malformed_success_response_is_not_marked_loaded(tmp_path):
    def handler(req):
        if req.url.path == "/mf":
            return httpx.Response(200, json=[{"schemeCode": 7, "schemeName": "bad source"}])
        return httpx.Response(200, json={"message": "upstream error disguised as success"})
    eng = get_engine(f"sqlite:///{tmp_path/'bad.db'}")
    st = run(load_all(eng, transport=httpx.MockTransport(handler), rps=1000, max_retries=0))
    assert st["ok"] == 0 and st["failed"] == [7]
    with eng.connect() as c:
        assert c.scalar(select(func.count()).select_from(schemes)) == 0
        assert c.scalar(select(func.count()).select_from(navs)) == 0


def test_loader_rejects_invalid_limits(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path/'limits.db'}")
    for kw in ({"concurrency": 0}, {"limit": -1}, {"max_age_hours": -1}):
        try:
            run(load_all(eng, **kw)); assert False
        except ValueError:
            pass

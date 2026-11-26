import pytest
from fastapi.testclient import TestClient
from app.db import get_engine
from app.ingest import compute_metrics, seed_demo
from app.main import create_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    eng = get_engine(f"sqlite:///{tmp_path_factory.mktemp('d')/'api.db'}")
    app = create_app(eng)
    with TestClient(app) as c:
        seed_demo(eng, n=9, years=6)
        assert compute_metrics(eng) == 9
        yield c


def test_list_filter_sort_paginate(client):
    r = client.get("/schemes", params={"category": "equity scheme - large cap fund", "page_size": 2, "sort_by": "ret_3y", "order": "desc"}).json()
    assert r["total"] == 3 and len(r["items"]) == 2 and r["pages"] == 2
    v = [i["metrics"]["ret_3y"] for i in r["items"]]
    assert v == sorted(v, reverse=True)
    assert client.get("/schemes", params={"page": 99}).json()["items"] == []
    assert client.get("/schemes", params={"sort_by": "bogus"}).status_code == 422
    assert client.get("/schemes", params={"page_size": 1000}).status_code == 422
    r = client.get("/schemes", params={"max_vol_3y": 1.0}).json()      # only debt funds are that calm
    assert r["total"] == 3 and all("Debt" in i["category"] for i in r["items"])


def test_scheme_endpoints(client):
    code = 100000
    assert client.get("/schemes/999").status_code == 404
    ret = client.get(f"/schemes/{code}/returns").json()
    assert ret["returns"]["1y"] is not None and ret["returns"]["10y"] is None
    risk = client.get(f"/schemes/{code}/risk", params={"period": "3y", "rf": 7}).json()
    assert risk["available"] and risk["max_drawdown"]["max_drawdown_pct"] <= 0
    assert client.get(f"/schemes/{code}/risk", params={"period": "10y"}).json()["available"] is False
    assert client.get(f"/schemes/{code}/risk", params={"period": "zzz"}).status_code == 422
    p1 = client.get(f"/schemes/{code}/rolling-returns", params={"window": "1y", "page_size": 100}).json()
    assert p1["summary"]["count"] == p1["total"] and len(p1["items"]) == 100
    p2 = client.get(f"/schemes/{code}/rolling-returns", params={"window": "1y", "page_size": 100, "page": 2}).json()
    assert p1["items"][-1]["date"] < p2["items"][0]["date"]
    nav = client.get(f"/schemes/{code}/nav", params={"from_date": "2026-01-01", "order": "desc"}).json()
    assert nav["total"] > 0 and nav["items"][0]["date"] >= nav["items"][-1]["date"]
    a = client.get(f"/schemes/{code}/analytics").json()
    assert set(a) >= {"returns", "risk", "rolling_returns_summary"}


def test_categories(client):
    r = client.get("/categories").json()
    assert r["total"] == 3 and all(c["active_count"] == 3 for c in r["items"])
    assert client.get("/categories", params={"name": "liquid"}).json()["total"] == 1
    assert client.get("/health").json()["status"] == "ok"


def test_category_analytics(client):
    cat = "Equity Scheme - Large Cap Fund"
    r = client.get("/categories/analytics", params={"category": cat.lower()}).json()
    assert r["constituents"] == 3 and r["returns"]["1y"] is not None
    assert r["risk"]["3y"]["available"] and r["risk"]["3y"]["max_drawdown"]["max_drawdown_pct"] <= 0
    assert r["rolling_returns_summary"]["1y"]["count"] > 0 and r["scheme_dispersion"]["ret_3y"]["count"] == 3
    p = client.get("/categories/rolling-returns", params={"category": cat, "window": "1y", "page_size": 50}).json()
    assert len(p["items"]) == 50 and p["summary"]["count"] == p["total"]
    assert client.get("/categories/analytics", params={"category": "nope"}).status_code == 404
    assert client.get("/categories/rolling-returns", params={"category": cat, "window": "x"}).status_code == 422
    assert client.get("/categories/analytics").status_code == 422


def test_data_quality_and_search_escaping(client):
    q = client.get("/schemes/100000/data-quality").json()
    assert q["available"] and 90 < q["coverage_pct"] <= 100 and q["gaps_over_threshold"]["count"] == 0
    assert client.get("/schemes/999/data-quality").status_code == 404
    assert client.get("/schemes", params={"q": "%"}).json()["total"] == 0      # '%' is literal, not a wildcard
    assert client.get("/schemes", params={"q": "demo fund 1"}).json()["total"] >= 1


def test_api_rate_limit(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path/'rl.db'}")
    with TestClient(create_app(eng, rate_limit_per_min=3)) as c:
        codes = [c.get("/schemes").status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        r = c.get("/schemes")
        assert int(r.headers["Retry-After"]) >= 1
        assert c.get("/health").status_code == 200        # health check is exempt


def test_search_matches_every_word(client):
    assert client.get("/schemes", params={"q": "demo 3"}).json()["total"] == 1      # words in any position
    assert client.get("/schemes", params={"q": "fund demo"}).json()["total"] == 9
    assert client.get("/schemes", params={"q": "demo nothing"}).json()["total"] == 0

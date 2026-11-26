"""Web UI is served by the API process, and the chart endpoints it relies on behave."""
import pytest
from fastapi.testclient import TestClient

from app.db import get_engine
from app.ingest import compute_metrics, seed_demo
from app.main import create_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    eng = get_engine(f"sqlite:///{tmp_path_factory.mktemp('f') / 'ui.db'}")
    with TestClient(create_app(eng, rate_limit_per_min=2)) as c:
        seed_demo(eng, n=6, years=6)
        compute_metrics(eng)
        yield c


def test_index_and_assets_served_and_not_rate_limited(client):
    for _ in range(4):                                   # limit is 2/min: UI routes must be exempt
        r = client.get("/")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert r.headers["cache-control"] == "no-cache" and '<script src="/static/app.js"' in r.text
        js = client.get("/static/app.js")
        assert js.status_code == 200 and js.headers["cache-control"] == "no-cache" and js.headers.get("etag")
        assert client.get("/static/app.css").status_code == 200
    assert client.get("/favicon.ico").headers["content-type"].startswith("image/svg")
    assert client.get("/static/nope.js").status_code == 404
    assert "/" not in client.get("/openapi.json").json()["paths"]        # UI routes hidden from API docs


def test_design_assets_served(client):
    html = client.get("/").text
    assert '<script src="/static/vendor/lottie_light.min.js" defer></script>' in html
    assert html.index("lottie_light.min.js") < html.index("/static/app.js")      # player loads before the app
    lot = client.get("/static/vendor/lottie_light.min.js")
    assert lot.status_code == 200 and "javascript" in lot.headers["content-type"]
    assert "eval(" not in lot.text                     # light build: no expressions engine
    assert client.get("/static/vendor/LICENSE-lottie-web.txt").status_code == 200
    for name in ("busy", "empty"):
        anim = client.get(f"/static/anim/{name}.json").json()
        assert anim["v"] and anim["fr"] == 60 and anim["op"] > anim["ip"] and anim["layers"]
    css = client.get("/static/app.css").text
    assert "prefers-reduced-motion" in css and '[data-theme="light"]' in css


def test_scheme_chart_downsamples_and_ranges(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'c.db'}")
    with TestClient(create_app(eng, rate_limit_per_min=0)) as c:
        seed_demo(eng, n=3, years=6)
        full = c.get("/schemes/100000/chart", params={"max_points": 100}).json()
        assert 50 <= len(full["nav"]) <= 101 and full["nav"][-1][0] == full["end"]
        one = c.get("/schemes/100000/chart", params={"range": "1y", "window": "3m", "max_points": 2000}).json()
        assert one["nav"][0][0] >= one["start"] and 230 <= len(one["nav"]) <= 260
        assert len(one["rolling"]) > 0 and one["window"] == "3m"
        assert c.get("/schemes/100000/chart", params={"range": "2y"}).status_code == 422
        assert c.get("/schemes/100000/chart", params={"max_points": 10}).status_code == 422
        assert c.get("/schemes/100000/chart", params={"window": "zz"}).status_code == 422
        assert c.get("/schemes/1/chart").status_code == 404
        cat = c.get("/categories/chart", params={"category": "Equity Scheme - Large Cap Fund", "range": "3y"}).json()
        assert cat["constituents"] == 1 and len(cat["nav"]) > 0

"""Tests for SIP/XIRR, plan classification, /compare, /sip, health, validation and limiter hardening."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import metrics as M
from app.db import classify_plan, get_engine, schemes
from app.ingest import compute_metrics, seed_demo, store_scheme
from app.main import create_app


# ---------------------------------------------------------------- pure functions
def test_xirr_simple_and_degenerate():
    t0 = pd.Timestamp("2023-01-01")
    assert M.xirr([(t0, -100), (t0 + pd.Timedelta(days=365), 110)]) == pytest.approx(0.10, abs=1e-6)
    assert M.xirr([(t0, -100), (t0 + pd.Timedelta(days=365), 50)]) == pytest.approx(-0.5, abs=1e-6)
    assert M.xirr([(t0, -100)]) is None
    assert M.xirr([(t0, -100), (t0 + pd.Timedelta(days=30), -100)]) is None     # no inflow


def test_sip_flat_nav_has_zero_return():
    days = pd.bdate_range("2022-01-03", "2024-12-31")
    s = M.make_series(days, np.full(len(days), 10.0))
    r = M.sip_returns(s, 1000, "2022-01-01", "2024-12-31", day=5)
    assert r["available"] and r["instalments"] == 36 and r["skipped_instalments"] == 0
    assert r["total_invested"] == 36000 and r["current_value"] == pytest.approx(36000)
    assert r["xirr_pct"] == pytest.approx(0, abs=1e-6)


def test_sip_xirr_matches_constant_growth_rate():
    days = pd.date_range("2020-01-01", "2024-12-31")
    s = M.make_series(days, [10 * 1.12 ** ((d - days[0]).days / 365) for d in days])
    r = M.sip_returns(s, 5000, "2020-01-01", day=10)
    assert r["xirr_pct"] == pytest.approx(12.0, abs=0.05)
    assert r["current_value"] > r["total_invested"]


def test_sip_skips_months_without_nav_and_handles_bad_ranges():
    d = list(pd.bdate_range("2024-01-01", "2024-02-28")) + list(pd.bdate_range("2024-05-01", "2024-06-28"))
    s = M.make_series(d, np.linspace(10, 12, len(d)))
    r = M.sip_returns(s, 100, "2024-01-01", "2024-06-28", day=1)
    assert r["instalments"] == 4 and r["skipped_instalments"] == 2          # March and April missing
    assert not M.sip_returns(s, 100, "2030-01-01")["available"]
    assert not M.sip_returns(M.make_series([], []), 100, "2024-01-01")["available"]


@pytest.mark.parametrize("name,plan,option", [
    ("Axis Bluechip Fund - Direct Plan - Growth", "direct", "growth"),
    ("HDFC Top 100 Fund - Regular Plan - IDCW Payout", "regular", "idcw"),
    ("Old Growth Fund - Dividend Option", "regular", "idcw"),
    ("Some Liquid Fund - Bonus", "regular", "bonus"),
    ("Legacy Scheme", "regular", None),
])
def test_classify_plan(name, plan, option):
    assert classify_plan(name) == {"plan": plan, "option": option}


# ---------------------------------------------------------------- API
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    eng = get_engine(f"sqlite:///{tmp_path_factory.mktemp('e') / 'enh.db'}")
    app = create_app(eng, rate_limit_per_min=0)
    with TestClient(app) as c:
        seed_demo(eng, n=6, years=6)
        days = pd.bdate_range(end=pd.Timestamp(date.today()), periods=800)
        for code, name in [(500, "Alpha Fund - Direct Plan - Growth"), (501, "Alpha Fund - Regular Plan - Growth"),
                           (502, "Alpha Fund - Direct Plan - IDCW")]:
            rows = [(d.date(), round(10 * 1.0004 ** i, 4)) for i, d in enumerate(days)]
            store_scheme(eng, code, name, {"scheme_category": "Equity Scheme - Large Cap Fund",
                                           "isin_growth": f"INF{code}"}, rows, 0)
        compute_metrics(eng)
        yield c


def test_scheme_out_has_plan_isin_and_metrics_date(client):
    r = client.get("/schemes/500").json()
    assert (r["plan"], r["option"], r["isin"]) == ("direct", "growth", "INF500")
    assert r["metrics_as_of"] == r["last_nav_date"]


def test_plan_and_option_filters(client):
    def codes(**p):
        return sorted(i["scheme_code"] for i in client.get("/schemes", params={"q": "alpha", **p}).json()["items"])
    assert codes(plan="direct") == [500, 502]
    assert codes(plan="regular") == [501]
    assert codes(option="growth") == [500, 501]
    assert codes(plan="direct", option="idcw") == [502]
    assert client.get("/schemes", params={"plan": "bogus"}).status_code == 422


def test_sip_endpoint(client):
    r = client.get("/schemes/100000/sip", params={"amount": 2000, "day": 15}).json()
    assert r["available"] and r["monthly_amount"] == 2000 and 34 <= r["instalments"] <= 37
    assert r["xirr_pct"] is not None
    assert client.get("/schemes/100000/sip", params={"day": 31}).status_code == 422
    assert client.get("/schemes/100000/sip", params={"amount": 0}).status_code == 422
    assert client.get("/schemes/100000/sip", params={"start": "2025-01-01", "end": "2024-01-01"}).status_code == 422
    assert client.get("/schemes/999/sip").status_code == 404


def test_compare_endpoint(client):
    r = client.get("/compare", params={"codes": "500,502,100000"}).json()
    assert [i["scheme_code"] for i in r["items"]] == [500, 502, 100000]
    assert all(i["returns"]["1y"] is not None for i in r["items"])
    assert any("IDCW" in w for w in r["warnings"])
    same = client.get("/compare", params={"codes": "500,501"}).json()
    assert same["warnings"] == [] and same["items"][0]["risk_3y"]["available"] is True    # 800 bdays > 3y
    past = client.get("/compare", params={"codes": "500,501", "as_of": str(date.today() - timedelta(days=400))}).json()
    assert all(i["nav_date"] <= past["as_of"] for i in past["items"])
    assert client.get("/compare", params={"codes": "500"}).status_code == 422
    assert client.get("/compare", params={"codes": "500,abc"}).status_code == 422
    assert client.get("/compare", params={"codes": "500,999"}).status_code == 404


def test_validation_gaps_closed(client):
    assert client.get("/schemes/100000/analytics", params={"rf": -1}).status_code == 422
    assert client.get("/categories/analytics",
                      params={"category": "Equity Scheme - Large Cap Fund", "rf": 99}).status_code == 422
    rng = {"from_date": "2025-01-02", "to_date": "2025-01-01"}
    assert client.get("/schemes/100000/nav", params=rng).status_code == 422
    assert client.get("/schemes/100000/rolling-returns", params=rng).status_code == 422


def test_health_reports_freshness(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["schemes"] == 9 and h["metrics"]["schemes"] == 9
    assert h["metrics"]["stale"] is False and h["latest_nav_date"]


def test_health_returns_503_when_db_unreachable(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'missing_dir' / 'x.db'}")
    r = TestClient(create_app(eng)).get("/health")      # no lifespan run; the DB file cannot be created
    assert r.status_code == 503 and r.json()["status"] == "error"


def test_forwarded_header_ignored_unless_trusted(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'fw.db'}")
    with TestClient(create_app(eng, rate_limit_per_min=2, trust_forwarded=False)) as c:
        codes = [c.get("/schemes", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(3)]
        assert codes == [200, 200, 429]                 # spoofed IPs don't get fresh buckets
    with TestClient(create_app(eng, rate_limit_per_min=2, trust_forwarded=True)) as c:
        codes = [c.get("/schemes", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(3)]
        assert codes == [200, 200, 200]


def test_compute_metrics_skips_scheme_without_navs(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'm.db'}")
    seed_demo(eng, n=3, years=2)
    with eng.begin() as c:                               # metadata claims history but rows are missing
        c.execute(schemes.insert().values(scheme_code=9, name="Ghost", nav_count=5, rejected_rows=0,
                                          last_nav_date=date.today()))
    assert compute_metrics(eng) == 3

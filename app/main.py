"""FastAPI application: scheme screener, per-scheme analytics, SIP simulation, comparison and
category-index analytics over the NAV history loaded by `app.ingest`."""
import logging
import math
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date, timedelta
from typing import Annotated, Generic, Literal, TypeVar

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.engine import Connection

from . import metrics as M
from .db import (ACTIVE_WINDOW_DAYS, IDCW_WORDS, METRIC_COLUMNS, classify_plan, get_engine, load_series,
                 metadata, navs, scheme_metrics, schemes)

log = logging.getLogger("api")
T = TypeVar("T")
VERSION = "1.1.0"
DESCRIPTION = """
Analytics over Indian mutual-fund NAV history (source: AMFI data via the mfapi.in mirror).

**Units:** returns, volatility and drawdown are in **percent**. Returns over periods longer than
one year are annualised (CAGR); shorter periods are absolute. NAVs are as published, so IDCW
(dividend) plans are price returns, not total returns. Missing NAV dates are bridged by using the
last NAV within 7 days; otherwise the metric is `null` (never guessed).

**Plan / option** labels (`direct`/`regular`, `growth`/`idcw`/`bonus`) are derived from scheme names.
"""
MAX_COMPARE = 10
STATIC_DIR = Path(__file__).parent / "static"
EXEMPT = ("/docs", "/redoc", "/openapi.json", "/health", "/", "/favicon.ico")


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    pages: int


class SchemeOut(BaseModel):
    scheme_code: int
    name: str
    fund_house: str | None = None
    scheme_type: str | None = None
    category: str | None = None
    isin: str | None = None
    plan: Literal["direct", "regular"]
    option: Literal["growth", "idcw", "bonus"] | None = None
    first_nav_date: date | None = None
    last_nav_date: date | None = None
    nav_count: int
    is_active: bool
    metrics_as_of: date | None = None
    metrics: dict[str, float | None] | None = None


class CategoryOut(BaseModel):
    category: str
    scheme_count: int
    active_count: int
    averages: dict[str, float | None]


def pager(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
    return page, page_size


Pg = Annotated[tuple[int, int], Depends(pager)]
RF = Annotated[float, Query(ge=0, le=50, description="Annual risk-free rate, %")]
Order = Literal["asc", "desc"]
SORTABLE = {"scheme_code": schemes.c.scheme_code, "name": schemes.c.name,
            **{c: scheme_metrics.c[c] for c in METRIC_COLUMNS}}
AVG_COLS = ["ret_1y", "ret_3y", "ret_5y", "vol_3y", "mdd_3y", "sharpe_3y"]
ANALYTIC_PERIODS = ("1y", "3y", "5y")


def _page(items, total, page, size):
    return {"items": items, "page": page, "page_size": size, "total": total, "pages": math.ceil(total / size)}


def _date_range(from_date: date | None, to_date: date | None):
    if from_date and to_date and from_date > to_date:
        raise HTTPException(422, "from_date must be on or before to_date")


def _series_page(r: pd.Series, from_date, to_date, order: str, page: int, size: int) -> tuple[pd.Series, dict]:
    """Slice a date-indexed return series by date range and paginate it."""
    if from_date:
        r = r.loc[pd.Timestamp(from_date):]
    if to_date:
        r = r.loc[: pd.Timestamp(to_date)]
    view = r.iloc[::-1] if order == "desc" else r
    part = view.iloc[(page - 1) * size: page * size]
    items = [{"date": d.date(), "return_pct": M._r(v)} for d, v in part.items()]
    return r, _page(items, len(r), page, size)


RANGES = {"1y": 1, "3y": 3, "5y": 5, "10y": 10, "max": None}
Range = Literal["1y", "3y", "5y", "10y", "max"]
MaxPts = Annotated[int, Query(ge=50, le=2000, description="Downsample to at most this many points")]


def _downsample(s: pd.Series, n: int) -> list[list]:
    """Evenly thin a series to <= n points, always keeping the last one (chart payloads stay small)."""
    if len(s) > n:
        step = math.ceil(len(s) / n)
        keep = s.iloc[::step]
        if keep.index[-1] != s.index[-1]:
            keep = pd.concat([keep, s.iloc[-1:]])
        s = keep
    return [[d.date().isoformat(), M._r(v)] for d, v in s.items()]


def _chart_payload(s: pd.Series, rng: str, months: int, n: int) -> dict:
    years = RANGES[rng]
    start = s.index[-1] - pd.DateOffset(years=years) if years else s.index[0]
    roll = M.rolling_returns(s, months)
    return {"range": rng, "start": max(start, s.index[0]).date(), "end": s.index[-1].date(),
            "nav": _downsample(s.loc[start:], n), "rolling": _downsample(roll.loc[start:], n)}


def _plan_filter(plan: str | None, option: str | None) -> list:
    """SQL twins of db.classify_plan, so filtering and labelling always agree."""
    name = func.lower(schemes.c.name)
    idcw = or_(*[name.contains(w, autoescape=True) for w in IDCW_WORDS])
    growth = name.contains("growth", autoescape=True)
    conds = []
    if plan == "direct":
        conds.append(name.contains("direct", autoescape=True))
    elif plan == "regular":
        conds.append(not_(name.contains("direct", autoescape=True)))
    if option == "idcw":
        conds.append(idcw)
    elif option == "growth":
        conds.append(and_(growth, not_(idcw)))
    elif option == "bonus":
        conds.append(and_(name.contains("bonus", autoescape=True), not_(growth), not_(idcw)))
    return conds


def _client_key(request: Request, trust_forwarded: bool) -> str:
    if trust_forwarded:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def create_app(engine=None, rate_limit_per_min: int | None = None, trust_forwarded: bool | None = None) -> FastAPI:
    engine = engine or get_engine()
    limit = int(os.getenv("RATE_LIMIT_PER_MIN", "300")) if rate_limit_per_min is None else rate_limit_per_min
    if trust_forwarded is None:
        trust_forwarded = os.getenv("TRUST_FORWARDED_FOR", "").lower() in ("1", "true", "yes")
    hits: dict[str, deque] = defaultdict(deque)
    last_sweep = [time.monotonic()]
    cat_cache: dict[tuple, tuple[float, object]] = {}

    @asynccontextmanager
    async def lifespan(_):
        metadata.create_all(engine)
        yield

    app = FastAPI(title="Mutual Fund Analytics API", version=VERSION, description=DESCRIPTION, lifespan=lifespan)

    origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"], allow_headers=["*"])

    @app.middleware("http")
    async def rate_limiter(request: Request, call_next):
        """Per-client sliding-window limiter (in-process; use a gateway/Redis when running several workers).

        X-Forwarded-For is only honoured when TRUST_FORWARDED_FOR is set, i.e. behind a proxy you control;
        otherwise any client could pick its own rate-limit key.
        """
        path = request.url.path
        if path.startswith("/static/"):
            resp = await call_next(request)
            resp.headers["Cache-Control"] = "no-cache"   # revalidate via ETag so a deploy is picked up at once
            return resp
        if limit <= 0 or path in EXEMPT:
            return await call_next(request)
        now = time.monotonic()
        if now - last_sweep[0] > 60:              # forget idle clients so memory stays bounded
            for k in [k for k, dq in hits.items() if not dq or now - dq[-1] > 60]:
                del hits[k]
            last_sweep[0] = now
        q = hits[_client_key(request, trust_forwarded)]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            wait = max(1, int(60 - (now - q[0])) + 1)
            return JSONResponse({"detail": "Rate limit exceeded"}, 429,
                                {"Retry-After": str(wait), "X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"})
        q.append(now)
        resp = await call_next(request)
        resp.headers["X-RateLimit-Limit"], resp.headers["X-RateLimit-Remaining"] = str(limit), str(limit - len(q))
        return resp

    if STATIC_DIR.is_dir():                     # web UI: same origin as the API, no build step
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

        @app.get("/favicon.ico", include_in_schema=False)
        def favicon():
            return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    def get_conn():
        with engine.connect() as c:
            yield c

    Conn = Annotated[Connection, Depends(get_conn)]

    def cutoff_date(conn):
        ref = conn.scalar(select(func.max(schemes.c.last_nav_date)))
        return (ref - timedelta(days=ACTIVE_WINDOW_DAYS)) if ref else date.min

    def scheme_or_404(conn, code):
        row = conn.execute(select(schemes).where(schemes.c.scheme_code == code)).mappings().first()
        if not row:
            raise HTTPException(404, f"Scheme {code} not found")
        return row

    def series_or_404(conn, code):
        row = scheme_or_404(conn, code)
        s = load_series(conn, code)
        if s.empty:
            raise HTTPException(404, f"Scheme {code} has no NAV history loaded")
        return row, s

    def period_or_422(text):
        try:
            return M.parse_period(text)
        except ValueError as e:
            raise HTTPException(422, str(e))

    def to_out(row, cutoff):
        m = {c: row[c] for c in METRIC_COLUMNS}
        return {**{k: row[k] for k in ("scheme_code", "name", "fund_house", "scheme_type", "category", "isin",
                                        "first_nav_date", "last_nav_date", "nav_count")},
                **classify_plan(row["name"]),
                "is_active": bool(row["last_nav_date"] and row["last_nav_date"] >= cutoff),
                "metrics_as_of": row.get("as_of"),
                "metrics": m if any(v is not None for v in m.values()) else None}

    MCOLS = [scheme_metrics.c.as_of, *[scheme_metrics.c[c] for c in METRIC_COLUMNS]]
    JOIN = schemes.outerjoin(scheme_metrics, schemes.c.scheme_code == scheme_metrics.c.scheme_code)

    # ------------------------------------------------------------ schemes
    @app.get("/schemes", response_model=Page[SchemeOut], tags=["Schemes"],
             summary="List/filter/sort schemes (category screener)")
    def list_schemes(
        conn: Conn, pg: Pg,
        q: Annotated[str | None, Query(description="Case-insensitive name search")] = None,
        category: Annotated[str | None, Query(description="Exact category (see /categories)")] = None,
        fund_house: str | None = None, scheme_type: str | None = None,
        plan: Annotated[Literal["direct", "regular"] | None, Query(description="Derived from scheme name")] = None,
        option: Annotated[Literal["growth", "idcw", "bonus"] | None, Query(description="Derived from scheme name")] = None,
        active: Annotated[bool | None, Query(description="Has a NAV within 10 days of the newest NAV in the DB")] = None,
        min_ret_1y: float | None = None, min_ret_3y: float | None = None,
        max_vol_3y: float | None = None, min_sharpe_3y: float | None = None,
        sort_by: Annotated[str, Query(description="scheme_code | name | " + " | ".join(METRIC_COLUMNS))] = "scheme_code",
        order: Order = "asc",
    ):
        if sort_by not in SORTABLE:
            raise HTTPException(422, f"sort_by must be one of {sorted(SORTABLE)}")
        cutoff = cutoff_date(conn)
        conds = _plan_filter(plan, option)
        if q: conds.append(schemes.c.name.icontains(q, autoescape=True))
        if category: conds.append(func.lower(schemes.c.category) == category.lower())
        if fund_house: conds.append(func.lower(schemes.c.fund_house) == fund_house.lower())
        if scheme_type: conds.append(func.lower(schemes.c.scheme_type) == scheme_type.lower())
        if active is True: conds.append(schemes.c.last_nav_date >= cutoff)
        if active is False: conds.append((schemes.c.last_nav_date < cutoff) | schemes.c.last_nav_date.is_(None))
        if min_ret_1y is not None: conds.append(scheme_metrics.c.ret_1y >= min_ret_1y)
        if min_ret_3y is not None: conds.append(scheme_metrics.c.ret_3y >= min_ret_3y)
        if max_vol_3y is not None: conds.append(scheme_metrics.c.vol_3y <= max_vol_3y)
        if min_sharpe_3y is not None: conds.append(scheme_metrics.c.sharpe_3y >= min_sharpe_3y)
        count_q = select(func.count()).select_from(JOIN).where(*conds)
        rows_q = select(*schemes.c, *MCOLS).select_from(JOIN).where(*conds)
        col = SORTABLE[sort_by]
        primary = (col.desc() if order == "desc" else col.asc()).nulls_last()
        page, size = pg
        rows = conn.execute(rows_q.order_by(primary, schemes.c.scheme_code)
                            .limit(size).offset((page - 1) * size)).mappings().all()
        return _page([to_out(r, cutoff) for r in rows], conn.scalar(count_q), page, size)

    @app.get("/schemes/{code}", response_model=SchemeOut, tags=["Schemes"])
    def get_scheme(code: int, conn: Conn):
        row = scheme_or_404(conn, code)
        m = conn.execute(select(*MCOLS).where(scheme_metrics.c.scheme_code == code)).mappings().first()
        return to_out({**row, **(m or {"as_of": None, **{c: None for c in METRIC_COLUMNS}})}, cutoff_date(conn))

    @app.get("/schemes/{code}/nav", tags=["Schemes"], summary="Raw NAV history (paginated)")
    def scheme_nav(code: int, conn: Conn, pg: Pg, from_date: date | None = None, to_date: date | None = None,
                   order: Order = "asc"):
        _date_range(from_date, to_date)
        scheme_or_404(conn, code)
        conds = [navs.c.scheme_code == code]
        if from_date: conds.append(navs.c.date >= from_date)
        if to_date: conds.append(navs.c.date <= to_date)
        page, size = pg
        total = conn.scalar(select(func.count()).select_from(navs).where(*conds))
        rows = conn.execute(select(navs.c.date, navs.c.nav).where(*conds)
                            .order_by(navs.c.date.desc() if order == "desc" else navs.c.date)
                            .limit(size).offset((page - 1) * size)).all()
        return _page([{"date": d, "nav": v} for d, v in rows], total, page, size)

    @app.get("/schemes/{code}/data-quality", tags=["Schemes"],
             summary="Coverage, gaps and rejected rows for a scheme's NAV history")
    def data_quality(code: int, conn: Conn):
        row, s = series_or_404(conn, code)
        return {"scheme_code": code, **M.data_quality(s, row["rejected_rows"] or 0)}

    # ------------------------------------------------------------ analytics
    @app.get("/schemes/{code}/returns", tags=["Analytics"],
             summary="Point-to-point returns: 1w, 1m, 3m, 6m, 1y, 3y, 5y, 10y, YTD, since inception")
    def returns(code: int, conn: Conn, as_of: Annotated[date | None, Query(description="Default: latest NAV")] = None):
        _, s = series_or_404(conn, code)
        if as_of:
            s = s.loc[: pd.Timestamp(as_of)]
            if s.empty:
                raise HTTPException(404, "No NAV on or before as_of")
        return {"scheme_code": code, "as_of": s.index[-1].date(), "unit": "percent",
                "returns": M.point_returns(s)}

    @app.get("/schemes/{code}/rolling-returns", tags=["Analytics"],
             summary="Rolling returns: summary statistics + paginated series")
    def rolling(code: int, conn: Conn, pg: Pg,
                window: Annotated[str, Query(description="e.g. 6m, 1y, 3y, 5y")] = "1y",
                from_date: date | None = None, to_date: date | None = None, order: Order = "asc"):
        _date_range(from_date, to_date)
        months = period_or_422(window)
        _, s = series_or_404(conn, code)
        r, body = _series_page(M.rolling_returns(s, months), from_date, to_date, order, *pg)
        return {"scheme_code": code, "window": window, "unit": "percent", "annualised": months > 12,
                "summary": M.rolling_summary(r), **body}

    @app.get("/schemes/{code}/risk", tags=["Analytics"], summary="Volatility, max drawdown, Sharpe over a period")
    def risk(code: int, conn: Conn, period: Annotated[str, Query(description="e.g. 1y, 3y, 5y")] = "3y",
             rf: RF = M.DEFAULT_RF):
        months = period_or_422(period)
        _, s = series_or_404(conn, code)
        return {"scheme_code": code, **M.risk_metrics(s, months, rf)}

    @app.get("/schemes/{code}/analytics", tags=["Analytics"], summary="Everything in one call")
    def analytics(code: int, conn: Conn, rf: RF = M.DEFAULT_RF):
        _, s = series_or_404(conn, code)
        return {"scheme_code": code, "as_of": s.index[-1].date(), "returns": M.point_returns(s),
                "risk": {p: M.risk_metrics(s, M.parse_period(p), rf) for p in ANALYTIC_PERIODS},
                "rolling_returns_summary": {w: M.rolling_summary(M.rolling_returns(s, M.parse_period(w)))
                                            for w in ANALYTIC_PERIODS}}

    @app.get("/schemes/{code}/chart", tags=["Analytics"],
             summary="Downsampled NAV and rolling-return series for charts")
    def scheme_chart(code: int, conn: Conn, range: Range = "max",
                     window: Annotated[str, Query(description="Rolling window, e.g. 1y, 3y")] = "1y",
                     max_points: MaxPts = 600):
        months = period_or_422(window)
        _, s = series_or_404(conn, code)
        return {"scheme_code": code, "window": window, **_chart_payload(s, range, months, max_points)}

    @app.get("/schemes/{code}/sip", tags=["Analytics"],
             summary="Monthly SIP simulation: invested amount, current value and XIRR")
    def sip(code: int, conn: Conn,
            amount: Annotated[float, Query(gt=0, le=10_000_000, description="Monthly instalment")] = 10_000,
            start: Annotated[date | None, Query(description="First instalment on/after this date (default: 3y before latest NAV)")] = None,
            end: Annotated[date | None, Query(description="Valuation date (default: latest NAV)")] = None,
            day: Annotated[int, Query(ge=1, le=28, description="Instalment day of month")] = 1):
        _date_range(start, end)
        _, s = series_or_404(conn, code)
        start = start or (s.index[-1] - pd.DateOffset(years=3)).date()
        res = M.sip_returns(s, amount, start, end, day)
        return {"scheme_code": code, "unit": "percent",
                "note": "Uses published NAVs; ignores stamp duty, exit load and taxes.", **res}

    @app.get("/compare", tags=["Analytics"], summary="Compare up to 10 schemes on a common as-of date")
    def compare(conn: Conn,
                codes: Annotated[str, Query(description="Comma-separated scheme codes, e.g. 119551,120503")],
                as_of: Annotated[date | None, Query(description="Default: earliest of the schemes' latest NAV dates")] = None,
                rf: RF = M.DEFAULT_RF):
        try:
            wanted = list(dict.fromkeys(int(c) for c in codes.split(",") if c.strip()))
        except ValueError:
            raise HTTPException(422, "codes must be comma-separated integers")
        if not 2 <= len(wanted) <= MAX_COMPARE:
            raise HTTPException(422, f"provide between 2 and {MAX_COMPARE} distinct scheme codes")
        rows = {r["scheme_code"]: r for r in conn.execute(
            select(schemes).where(schemes.c.scheme_code.in_(wanted))).mappings()}
        missing = [c for c in wanted if c not in rows]
        if missing:
            raise HTTPException(404, f"Schemes not found: {missing}")
        series = {c: load_series(conn, c) for c in wanted}
        empty = [c for c, s in series.items() if s.empty]
        if empty:
            raise HTTPException(404, f"Schemes with no NAV history loaded: {empty}")
        common = pd.Timestamp(as_of) if as_of else min(s.index[-1] for s in series.values())
        items = []
        for c in wanted:
            s = series[c].loc[:common]
            r3 = M.risk_metrics(s, 36, rf) if not s.empty else {"available": False, "reason": "no NAV on or before as_of"}
            items.append({
                "scheme_code": c, "name": rows[c]["name"], "category": rows[c]["category"],
                **classify_plan(rows[c]["name"]),
                "nav_date": s.index[-1].date() if not s.empty else None,
                "returns": M.point_returns(s) if not s.empty else None,
                "risk_3y": {k: r3.get(k) for k in ("available", "reason", "volatility_pct", "sharpe_ratio")}
                | {"max_drawdown_pct": (r3.get("max_drawdown") or {}).get("max_drawdown_pct")},
            })
        cats = {i["category"] for i in items}
        return {"as_of": common.date(), "unit": "percent", "risk_free_rate_pct": rf,
                "warnings": (["schemes are from different categories"] if len(cats) > 1 else [])
                + (["mixes growth and IDCW options; IDCW returns exclude payouts"]
                   if {"growth", "idcw"} <= {i["option"] for i in items} else []),
                "items": items}

    # ------------------------------------------------------------ categories
    @app.get("/categories", response_model=Page[CategoryOut], tags=["Categories"],
             summary="Categories with equal-weighted average metrics of their active schemes")
    def categories(conn: Conn, pg: Pg, name: Annotated[str | None, Query(description="Substring filter")] = None):
        cutoff = cutoff_date(conn)
        q = (select(schemes.c.category, func.count().label("scheme_count"),
                    func.sum(case((schemes.c.last_nav_date >= cutoff, 1), else_=0)).label("active_count"),
                    *[func.avg(scheme_metrics.c[c]).label(c) for c in AVG_COLS])
             .select_from(JOIN).where(schemes.c.category.isnot(None)).group_by(schemes.c.category))
        if name:
            q = q.where(schemes.c.category.icontains(name, autoescape=True))
        page, size = pg
        total = conn.scalar(select(func.count()).select_from(q.subquery()))
        rows = conn.execute(q.order_by(schemes.c.category).limit(size).offset((page - 1) * size)).mappings().all()
        items = [{"category": r["category"], "scheme_count": r["scheme_count"], "active_count": int(r["active_count"] or 0),
                  "averages": {f"avg_{c}": M._r(r[c]) for c in AVG_COLS}} for r in rows]
        return _page(items, total, page, size)

    def category_series(conn, category: str, max_schemes: int):
        ref = conn.scalar(select(func.max(schemes.c.last_nav_date)))
        key = (category.lower(), max_schemes, ref)
        hit = cat_cache.get(key)
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        codes = [r[0] for r in conn.execute(
            select(schemes.c.scheme_code).where(func.lower(schemes.c.category) == category.lower(),
                                                schemes.c.last_nav_date >= cutoff_date(conn), schemes.c.nav_count > 1)
            .order_by(schemes.c.nav_count.desc(), schemes.c.scheme_code).limit(max_schemes))]
        if not codes:
            raise HTTPException(404, f"No active schemes in category '{category}' (see /categories)")
        idx = M.category_index([load_series(conn, c) for c in codes])
        if idx.empty:
            raise HTTPException(404, f"Not enough overlapping NAV history to build an index for '{category}'")
        if len(cat_cache) > 50:
            cat_cache.clear()
        cat_cache[key] = (time.monotonic(), (idx, len(codes)))
        return idx, len(codes)

    CAT_NOTE = ("Equal-weighted, daily-rebalanced index of the active schemes in the category (longest histories "
                "first, up to max_schemes). Survivorship bias: closed/merged schemes are excluded.")
    Cat = Annotated[str, Query(description="Exact category name (see /categories)")]
    MaxS = Annotated[int, Query(ge=3, le=500, description="Max schemes used to build the category index")]

    @app.get("/categories/analytics", tags=["Categories"],
             summary="Category returns, rolling returns, volatility, drawdown and Sharpe")
    def category_analytics(category: Cat, conn: Conn, max_schemes: MaxS = 100, rf: RF = M.DEFAULT_RF):
        idx, n = category_series(conn, category, max_schemes)
        dist = conn.execute(select(*MCOLS).select_from(JOIN).where(
            func.lower(schemes.c.category) == category.lower(), schemes.c.last_nav_date >= cutoff_date(conn))).mappings().all()
        spread = {}
        for c in ("ret_1y", "ret_3y", "vol_3y", "sharpe_3y"):
            v = pd.Series([r[c] for r in dist if r[c] is not None], dtype=float)
            spread[c] = {"count": int(len(v)), "min": M._r(v.min()) if len(v) else None,
                         "median": M._r(v.median()) if len(v) else None, "max": M._r(v.max()) if len(v) else None}
        return {"category": category, "constituents": n, "as_of": idx.index[-1].date(), "methodology": CAT_NOTE,
                "returns": M.point_returns(idx),
                "risk": {p: M.risk_metrics(idx, M.parse_period(p), rf) for p in ANALYTIC_PERIODS},
                "rolling_returns_summary": {w: M.rolling_summary(M.rolling_returns(idx, M.parse_period(w)))
                                            for w in ANALYTIC_PERIODS},
                "scheme_dispersion": spread}

    @app.get("/categories/rolling-returns", tags=["Categories"], summary="Category rolling returns (paginated series)")
    def category_rolling(category: Cat, conn: Conn, pg: Pg, window: str = "1y", max_schemes: MaxS = 100,
                         from_date: date | None = None, to_date: date | None = None, order: Order = "asc"):
        _date_range(from_date, to_date)
        months = period_or_422(window)
        idx, n = category_series(conn, category, max_schemes)
        r, body = _series_page(M.rolling_returns(idx, months), from_date, to_date, order, *pg)
        return {"category": category, "constituents": n, "window": window, "unit": "percent",
                "annualised": months > 12, "methodology": CAT_NOTE, "summary": M.rolling_summary(r), **body}

    @app.get("/categories/chart", tags=["Categories"],
             summary="Downsampled category-index and rolling-return series for charts")
    def category_chart(category: Cat, conn: Conn, range: Range = "max", window: str = "1y",
                       max_schemes: MaxS = 100, max_points: MaxPts = 600):
        months = period_or_422(window)
        idx, n = category_series(conn, category, max_schemes)
        return {"category": category, "constituents": n, "window": window,
                **_chart_payload(idx, range, months, max_points)}

    # ------------------------------------------------------------ meta
    @app.get("/health", tags=["Meta"], summary="Database reachability and data freshness")
    def health():
        try:
            with engine.connect() as conn:
                n = conn.scalar(select(func.count()).select_from(schemes))
                latest = conn.scalar(select(func.max(schemes.c.last_nav_date)))
                m_n = conn.scalar(select(func.count()).select_from(scheme_metrics))
                m_asof = conn.scalar(select(func.max(scheme_metrics.c.as_of)))
        except Exception as e:  # noqa: BLE001 - report, don't crash the probe
            log.warning("health check failed: %s", e)
            return JSONResponse({"status": "error", "detail": "database unavailable"}, 503)
        return {"status": "ok", "version": VERSION, "schemes": n, "latest_nav_date": latest,
                "metrics": {"schemes": m_n, "as_of": m_asof,
                            "stale": bool(latest and (m_asof is None or m_asof < latest))}}

    return app


app = create_app()

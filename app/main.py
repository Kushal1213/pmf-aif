
import math
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Generic, Literal, TypeVar

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.engine import Connection

import pandas as _pd

from . import metrics as M
from .db import METRIC_COLUMNS, get_engine, load_series, metadata, navs, scheme_metrics, schemes

ACTIVE_WINDOW_DAYS = 10
T = TypeVar("T")
pd_series = lambda v: _pd.Series(v, dtype=float)
DESCRIPTION = """
Analytics over Indian mutual-fund NAV history (source: AMFI via mfapi.in).

**Units:** returns, volatility and drawdown are in **percent**. Returns over periods longer than
one year are annualised (CAGR); shorter periods are absolute. NAVs are as published, so IDCW
(dividend) plans are price returns, not total returns. Missing NAV dates are bridged by using the
last NAV within 7 days; otherwise the metric is `null` (never guessed).
"""


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
    first_nav_date: date | None = None
    last_nav_date: date | None = None
    nav_count: int
    is_active: bool
    metrics: dict[str, float | None] | None = None


class CategoryOut(BaseModel):
    category: str
    scheme_count: int
    active_count: int
    averages: dict[str, float | None]


def pager(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
    return page, page_size


Pg = Annotated[tuple[int, int], Depends(pager)]
SORTABLE = {"scheme_code": schemes.c.scheme_code, "name": schemes.c.name,
            **{c: scheme_metrics.c[c] for c in METRIC_COLUMNS}}
AVG_COLS = ["ret_1y", "ret_3y", "ret_5y", "vol_3y", "mdd_3y", "sharpe_3y"]


def _page(items, total, page, size):
    return {"items": items, "page": page, "page_size": size, "total": total, "pages": math.ceil(total / size)}


def create_app(engine=None, rate_limit_per_min: int | None = None) -> FastAPI:
    engine = engine or get_engine()
    limit = int(os.getenv("RATE_LIMIT_PER_MIN", "300")) if rate_limit_per_min is None else rate_limit_per_min
    hits: dict[str, deque] = defaultdict(deque)
    cat_cache: dict[tuple, tuple[float, object]] = {}

    @asynccontextmanager
    async def lifespan(_):
        metadata.create_all(engine)
        yield

    app = FastAPI(title="Mutual Fund Analytics API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)

    @app.middleware("http")
    async def rate_limiter(request: Request, call_next):
        """Per-client sliding-window limiter (in-process; use a gateway/Redis when running several workers)."""
        if limit <= 0 or request.url.path in ("/docs", "/redoc", "/openapi.json", "/health"):
            return await call_next(request)
        key = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
        now, q = time.monotonic(), hits[key]
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
        return {**{k: row[k] for k in ("scheme_code", "name", "fund_house", "scheme_type", "category",
                                        "first_nav_date", "last_nav_date", "nav_count")},
                "is_active": bool(row["last_nav_date"] and row["last_nav_date"] >= cutoff),
                "metrics": m if any(v is not None for v in m.values()) else None}

    MCOLS = [scheme_metrics.c[c] for c in METRIC_COLUMNS]
    JOIN = schemes.outerjoin(scheme_metrics, schemes.c.scheme_code == scheme_metrics.c.scheme_code)

    # ------------------------------------------------------------ schemes
    @app.get("/schemes", response_model=Page[SchemeOut], tags=["Schemes"],
             summary="List/filter/sort schemes (category screener)")
    def list_schemes(
        conn: Conn, pg: Pg,
        q: Annotated[str | None, Query(description="Case-insensitive name search")] = None,
        category: Annotated[str | None, Query(description="Exact category (see /categories)")] = None,
        fund_house: str | None = None, scheme_type: str | None = None,
        active: Annotated[bool | None, Query(description="Has a NAV within 10 days of the newest NAV in the DB")] = None,
        min_ret_1y: float | None = None, min_ret_3y: float | None = None,
        max_vol_3y: float | None = None, min_sharpe_3y: float | None = None,
        sort_by: Annotated[str, Query(description="scheme_code | name | " + " | ".join(METRIC_COLUMNS))] = "scheme_code",
        order: Literal["asc", "desc"] = "asc",
    ):
        if sort_by not in SORTABLE:
            raise HTTPException(422, f"sort_by must be one of {sorted(SORTABLE)}")
        cutoff = cutoff_date(conn)
        conds = []
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
        count_q = select(func.count()).select_from(JOIN)
        rows_q = select(*schemes.c, *MCOLS).select_from(JOIN)
        for c in conds:
            count_q, rows_q = count_q.where(c), rows_q.where(c)
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
        return to_out({**row, **(m or {c: None for c in METRIC_COLUMNS})}, cutoff_date(conn))

    @app.get("/schemes/{code}/nav", tags=["Schemes"], summary="Raw NAV history (paginated)")
    def scheme_nav(code: int, conn: Conn, pg: Pg, from_date: date | None = None, to_date: date | None = None,
                   order: Literal["asc", "desc"] = "asc"):
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

    # ------------------------------------------------------------ analytics
    @app.get("/schemes/{code}/returns", tags=["Analytics"],
             summary="Point-to-point returns: 1w, 1m, 3m, 6m, 1y, 3y, 5y, 10y, YTD, since inception")
    def returns(code: int, conn: Conn, as_of: Annotated[date | None, Query(description="Default: latest NAV")] = None):
        _, s = series_or_404(conn, code)
        if as_of:
            s = s.loc[: M.pd.Timestamp(as_of)]
            if s.empty:
                raise HTTPException(404, "No NAV on or before as_of")
        return {"scheme_code": code, "as_of": s.index[-1].date(), "unit": "percent",
                "returns": M.point_returns(s)}

    @app.get("/schemes/{code}/rolling-returns", tags=["Analytics"],
             summary="Rolling returns: summary statistics + paginated series")
    def rolling(code: int, conn: Conn, pg: Pg,
                window: Annotated[str, Query(description="e.g. 6m, 1y, 3y, 5y")] = "1y",
                from_date: date | None = None, to_date: date | None = None,
                order: Literal["asc", "desc"] = "asc"):
        months = period_or_422(window)
        _, s = series_or_404(conn, code)
        r = M.rolling_returns(s, months)            # computed on full history, then sliced
        if from_date: r = r.loc[M.pd.Timestamp(from_date):]
        if to_date: r = r.loc[: M.pd.Timestamp(to_date)]
        page, size = pg
        view = r.iloc[::-1] if order == "desc" else r
        part = view.iloc[(page - 1) * size: page * size]
        return {"scheme_code": code, "window": window, "unit": "percent", "annualised": months > 12,
                "summary": M.rolling_summary(r),
                **_page([{"date": d.date(), "return_pct": M._r(v)} for d, v in part.items()], len(r), page, size)}

    @app.get("/schemes/{code}/risk", tags=["Analytics"], summary="Volatility, max drawdown, Sharpe over a period")
    def risk(code: int, conn: Conn, period: Annotated[str, Query(description="e.g. 1y, 3y, 5y")] = "3y",
             rf: Annotated[float, Query(ge=0, le=50, description="Annual risk-free rate, %")] = M.DEFAULT_RF):
        months = period_or_422(period)
        _, s = series_or_404(conn, code)
        return {"scheme_code": code, **M.risk_metrics(s, months, rf)}

    @app.get("/schemes/{code}/analytics", tags=["Analytics"], summary="Everything in one call")
    def analytics(code: int, conn: Conn, rf: float = M.DEFAULT_RF):
        _, s = series_or_404(conn, code)
        return {"scheme_code": code, "as_of": s.index[-1].date(), "returns": M.point_returns(s),
                "risk": {p: M.risk_metrics(s, M.parse_period(p), rf) for p in ("1y", "3y", "5y")},
                "rolling_returns_summary": {w: M.rolling_summary(M.rolling_returns(s, M.parse_period(w)))
                                            for w in ("1y", "3y", "5y")}}

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

    @app.get("/schemes/{code}/data-quality", tags=["Schemes"],
             summary="Coverage, gaps and rejected rows for a scheme's NAV history")
    def data_quality(code: int, conn: Conn):
        row, s = series_or_404(conn, code)
        return {"scheme_code": code, **M.data_quality(s, row["rejected_rows"] or 0)}

    # ------------------------------------------------------------ category-level analytics
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
    def category_analytics(category: Cat, conn: Conn, max_schemes: MaxS = 100, rf: float = M.DEFAULT_RF):
        idx, n = category_series(conn, category, max_schemes)
        dist = conn.execute(select(*MCOLS).select_from(JOIN).where(
            func.lower(schemes.c.category) == category.lower(), schemes.c.last_nav_date >= cutoff_date(conn))).mappings().all()
        spread = {}
        for c in ("ret_1y", "ret_3y", "vol_3y", "sharpe_3y"):
            v = pd_series([r[c] for r in dist if r[c] is not None])
            spread[c] = {"count": int(len(v)), "min": M._r(v.min()) if len(v) else None,
                         "median": M._r(v.median()) if len(v) else None, "max": M._r(v.max()) if len(v) else None}
        return {"category": category, "constituents": n, "as_of": idx.index[-1].date(), "methodology": CAT_NOTE,
                "returns": M.point_returns(idx),
                "risk": {p: M.risk_metrics(idx, M.parse_period(p), rf) for p in ("1y", "3y", "5y")},
                "rolling_returns_summary": {w: M.rolling_summary(M.rolling_returns(idx, M.parse_period(w)))
                                            for w in ("1y", "3y", "5y")},
                "scheme_dispersion": spread}

    @app.get("/categories/rolling-returns", tags=["Categories"], summary="Category rolling returns (paginated series)")
    def category_rolling(category: Cat, conn: Conn, pg: Pg, window: str = "1y", max_schemes: MaxS = 100,
                         from_date: date | None = None, to_date: date | None = None,
                         order: Literal["asc", "desc"] = "asc"):
        months = period_or_422(window)
        idx, n = category_series(conn, category, max_schemes)
        r = M.rolling_returns(idx, months)
        if from_date: r = r.loc[M.pd.Timestamp(from_date):]
        if to_date: r = r.loc[: M.pd.Timestamp(to_date)]
        page, size = pg
        view = r.iloc[::-1] if order == "desc" else r
        part = view.iloc[(page - 1) * size: page * size]
        return {"category": category, "constituents": n, "window": window, "unit": "percent",
                "annualised": months > 12, "methodology": CAT_NOTE, "summary": M.rolling_summary(r),
                **_page([{"date": d.date(), "return_pct": M._r(v)} for d, v in part.items()], len(r), page, size)}

    @app.get("/health", tags=["Meta"])
    def health(conn: Conn):
        return {"status": "ok", "schemes": conn.scalar(select(func.count()).select_from(schemes))}

    return app


app = create_app()

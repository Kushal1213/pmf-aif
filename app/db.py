from __future__ import annotations

import os

from sqlalchemy import (Column, Date, DateTime, Float, ForeignKey, Index, Integer, MetaData,
                        String, Table, create_engine, event, select)

from .metrics import make_series

METRIC_COLUMNS = ["ret_1m", "ret_3m", "ret_6m", "ret_1y", "ret_3y", "ret_5y",
                  "vol_1y", "vol_3y", "mdd_1y", "mdd_3y", "sharpe_1y", "sharpe_3y"]

metadata = MetaData()

schemes = Table(
    "schemes", metadata,
    Column("scheme_code", Integer, primary_key=True, autoincrement=False),
    Column("name", String, nullable=False),
    Column("fund_house", String), Column("scheme_type", String), Column("category", String),
    Column("isin", String),
    Column("first_nav_date", Date), Column("last_nav_date", Date),
    Column("nav_count", Integer, nullable=False, default=0),
    Column("rejected_rows", Integer, nullable=False, default=0),
    Column("loaded_at", DateTime),
    Index("ix_schemes_category", "category"), Index("ix_schemes_fund_house", "fund_house"),
)

# Composite PK doubles as the (scheme, date) range-scan index. ~10M+ rows at full history.
navs = Table(
    "navs", metadata,
    Column("scheme_code", Integer, ForeignKey("schemes.scheme_code"), primary_key=True, autoincrement=False),
    Column("date", Date, primary_key=True),
    Column("nav", Float, nullable=False),
    sqlite_with_rowid=False,
)

# Precomputed so category-wide filter/sort/paginate is a single indexed SQL query.
scheme_metrics = Table(
    "scheme_metrics", metadata,
    Column("scheme_code", Integer, ForeignKey("schemes.scheme_code"), primary_key=True, autoincrement=False),
    Column("as_of", Date),
    *[Column(c, Float) for c in METRIC_COLUMNS],
)


def get_engine(url: str | None = None):
    url = url or os.getenv("DATABASE_URL", "sqlite:///mf.db")
    eng = create_engine(url, pool_pre_ping=True)
    if eng.dialect.name == "sqlite":
        @event.listens_for(eng, "connect")
        def _pragmas(dbapi, _):
            cur = dbapi.cursor()
            for p in ("journal_mode=WAL", "synchronous=NORMAL", "busy_timeout=30000"):
                cur.execute(f"PRAGMA {p}")
            cur.close()
    return eng


def upsert(conn, table, rows, keys, update_cols):
    if not rows:
        return
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(table)
    stmt = stmt.on_conflict_do_update(index_elements=keys, set_={c: stmt.excluded[c] for c in update_cols})
    conn.execute(stmt, rows)


def load_series(conn, code: int):
    rows = conn.execute(select(navs.c.date, navs.c.nav).where(navs.c.scheme_code == code)
                        .order_by(navs.c.date)).all()
    return make_series([r[0] for r in rows], [r[1] for r in rows])

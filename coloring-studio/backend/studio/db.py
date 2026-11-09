"""Database engine, sessions and versioned migrations.

SQLite is used for local development (WAL mode, safe for the API + worker
threads); set DATABASE_URL=postgresql+psycopg://... for production.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
        return eng
    return create_engine(url, pool_pre_ping=True)


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def SessionLocal() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------------------------------------------------------------- migrations
# Each migration is (version, description, fn(conn)). Append new ones; never edit
# an applied migration. v1 creates the initial schema from the ORM metadata.

def _m1_initial(conn) -> None:
    Base.metadata.create_all(conn)


MIGRATIONS: list[tuple[int, str, Callable]] = [
    (1, "initial schema", _m1_initial),
]


def migrate() -> int:
    """Apply pending migrations; returns the resulting schema version."""
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, description VARCHAR(200) NOT NULL)"))
        applied = {r[0] for r in conn.execute(text("SELECT version FROM schema_migrations"))}
    current = max(applied) if applied else 0
    for version, desc, fn in MIGRATIONS:
        if version in applied:
            continue
        with eng.begin() as conn:
            fn(conn)
            conn.execute(text("INSERT INTO schema_migrations (version, description) VALUES (:v, :d)"),
                         {"v": version, "d": desc})
        current = version
    return current


def table_names() -> list[str]:
    return inspect(get_engine()).get_table_names()

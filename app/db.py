"""Database session management."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

_engine = None
_SessionLocal = None

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings().database_url,
            pool_pre_ping=True,
            future=True,
            # Defence in depth for the audit chain: never let a connection's
            # local timezone leak into a stored or compared timestamp.
            connect_args={"options": "-c timezone=UTC"},
        )
    return _engine


def session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def apply_migrations() -> None:
    for path in sorted(MIGRATIONS.glob("*.sql")):
        with engine().begin() as conn:
            conn.execute(text(path.read_text()))


def reset_engine() -> None:
    """Test hook."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None

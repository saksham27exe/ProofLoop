"""SQLite connection management for ProofLoop's evidence store."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import IMMUTABLE_TABLES, Base


def _set_sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - driver hook
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    """Owns the SQLAlchemy engine/session factory for ``proofloop.db``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )
        event.listen(self.engine, "connect", _set_sqlite_pragmas)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._install_immutability_triggers()

    def _install_immutability_triggers(self) -> None:
        with self.engine.begin() as conn:
            for table in IMMUTABLE_TABLES:
                for op in ("UPDATE", "DELETE"):
                    conn.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{op.lower()} "
                            f"BEFORE {op} ON {table} BEGIN "
                            f"SELECT RAISE(ABORT, '{table} is append-only evidence; {op} is not permitted'); END;"
                        )
                    )

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commit on success, roll back on error."""
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def new_session(self) -> Session:
        return self._factory()

    def dispose(self) -> None:
        self.engine.dispose()

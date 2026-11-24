"""MySQL access for the stats database.

The connection comes from the ``DATABASE_URL`` environment variable, e.g.
``mysql+pymysql://user:password@host:3306/rummy_wars`` (``mysql://`` also works).
Queries use the PyMySQL ``%s`` placeholder style.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, exc
from sqlalchemy.engine import URL, Connection as SAConnection, make_url
from sqlalchemy.types import String

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

Error = exc.SQLAlchemyError
# A missing table or column; connection failures are raised by ``connect`` instead.
SchemaError = (exc.ProgrammingError, exc.OperationalError)
# Length of text columns written by ``write_frame``; bounded so they can be indexed.
TEXT_LENGTH = 255
logger = logging.getLogger(__name__)


class NotConfigured(RuntimeError):
    """``DATABASE_URL`` is not set."""


def database_url() -> URL:
    """The SQLAlchemy URL from ``DATABASE_URL``, defaulting to the PyMySQL driver and utf8mb4.

    Returns:
        The parsed URL.

    Raises:
        NotConfigured: If ``DATABASE_URL`` is unset or blank.
    """
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        raise NotConfigured("DATABASE_URL is not set, e.g. mysql+pymysql://user:password@host:3306/rummy_wars")
    url = make_url(raw)
    if url.drivername in ("mysql", "mariadb"):
        url = url.set(drivername="mysql+pymysql")
    if "charset" not in url.query:
        url = url.update_query_dict({"charset": "utf8mb4"})
    return url


@lru_cache(maxsize=None)
def _engine(url: URL):
    """The shared engine for ``url``; cached so connections are pooled across requests."""
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800)


class Row(tuple):
    """A result row readable by position or column name."""

    _names: tuple[str, ...]

    def __new__(cls, values: Iterable, names: Iterable[str]):
        """Create a row from ``values`` labelled with ``names`` (same order)."""
        row = super().__new__(cls, values)
        row._names = tuple(names)
        return row

    def __getitem__(self, key):
        """Return a value by position or column name.

        Raises:
            KeyError: If ``key`` is a string that is not a column name.
        """
        if isinstance(key, str):
            try:
                return super().__getitem__(self._names.index(key))
            except ValueError:
                raise KeyError(key) from None
        return super().__getitem__(key)

    def keys(self) -> list[str]:
        """The column names, so a row converts with ``dict(row)``."""
        return list(self._names)


class Result:
    """A statement result whose rows are ``Row`` objects; statements without rows yield none."""

    def __init__(self, result) -> None:
        """Wrap a SQLAlchemy result, capturing its column names up front."""
        self._result = result
        self._names = tuple(result.keys()) if result.returns_rows else ()

    def fetchone(self) -> Row | None:
        """The next row, or None when there are no more."""
        row = self._result.fetchone() if self._names else None
        return Row(row, self._names) if row is not None else None

    def fetchall(self) -> list[Row]:
        """All remaining rows."""
        return [Row(row, self._names) for row in self._result.fetchall()] if self._names else []

    def __iter__(self) -> Iterator[Row]:
        """Iterate over all remaining rows."""
        return iter(self.fetchall())


class Connection:
    """A MySQL connection; as a context manager it commits on success and always closes."""

    def __init__(self, sa_connection: SAConnection) -> None:
        """Wrap an open SQLAlchemy connection."""
        self._sa = sa_connection

    def execute(self, sql: str, params: Iterable = ()) -> Result:
        """Run one statement with ``%s`` placeholders bound to ``params``."""
        params = tuple(params)
        return Result(self._sa.exec_driver_sql(sql, params) if params else self._sa.exec_driver_sql(sql))

    def executemany(self, sql: str, rows: Iterable[Iterable]) -> None:
        """Run ``sql`` once per row of parameters; does nothing for no rows."""
        rows = [tuple(row) for row in rows]
        if rows:
            self._sa.exec_driver_sql(sql, rows)

    def executescript(self, script: str) -> None:
        """Run semicolon-separated statements (none may contain a literal semicolon)."""
        for statement in script.split(";"):
            if statement.strip():
                self._sa.exec_driver_sql(statement)

    def read_sql(self, sql: str, params: Iterable | None = None) -> pd.DataFrame:
        """Run a query and return the result as a DataFrame."""
        return pd.read_sql_query(sql, self._sa, params=tuple(params) if params else None)

    def write_frame(self, df: pd.DataFrame, table: str) -> None:
        """Replace ``table`` with ``df``; text columns become indexable ``VARCHAR`` columns."""
        text_columns = {
            column: String(TEXT_LENGTH)
            for column in df.columns
            if df[column].dtype == object or pd.api.types.is_string_dtype(df[column])
        }
        df.to_sql(table, self._sa, if_exists="replace", index=False, dtype=text_columns)

    def commit(self) -> None:
        """Commit the current transaction."""
        self._sa.commit()

    def rollback(self) -> None:
        """Roll back the current transaction."""
        self._sa.rollback()

    def close(self) -> None:
        """Return the connection to the pool."""
        self._sa.close()

    def __enter__(self) -> Connection:
        """Enter the ``with`` block, returning this connection."""
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """Roll back on an exception, otherwise commit; always close."""
        try:
            self.rollback() if exc_type else self.commit()
        finally:
            self.close()


def connect() -> Connection:
    """Open a connection to the database named by ``DATABASE_URL``."""
    return Connection(_engine(database_url()).connect())


def connect_or_none() -> Connection | None:
    """Like ``connect``, but None (and a logged warning) when the database is not configured or reachable."""
    try:
        return connect()
    except (NotConfigured, Error) as error:
        logger.warning("Stats database unavailable: %s", error)
        return None


def table_exists(conn: Connection, table: str) -> bool:
    """True if ``table`` exists in the connected schema."""
    return conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s", (table,)
    ).fetchone() is not None


def column_names(conn: Connection, table: str) -> list[str]:
    """Columns of ``table`` in order, or an empty list if it does not exist."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [row[0] for row in rows]


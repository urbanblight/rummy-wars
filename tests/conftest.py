"""Database tests run against a MySQL database named by ``TEST_DATABASE_URL``.

Every table in that database is dropped before each test that uses the ``database`` fixture,
so point it at a throwaway schema (its name must contain "test"), for example::

    docker run -d -e MYSQL_ROOT_PASSWORD=test -e MYSQL_DATABASE=rummy_wars_test -p 33306:3306 mysql:8.4
    export TEST_DATABASE_URL=mysql+pymysql://root:test@127.0.0.1:33306/rummy_wars_test
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import zscores  # noqa: E402


def _drop_all_tables(conn: db.Connection) -> None:
    tables = [row[0] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()"
    ).fetchall()]
    conn.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in tables:
        conn.execute(f"DROP TABLE `{table}`")
    conn.execute("SET FOREIGN_KEY_CHECKS = 1")


@pytest.fixture
def database(monkeypatch):
    """An empty MySQL test database; ``DATABASE_URL`` points at it for the test."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    monkeypatch.setenv("DATABASE_URL", url)
    if "test" not in db.database_url().database.lower():
        pytest.fail("TEST_DATABASE_URL must name a database containing 'test'; its tables are dropped")
    monkeypatch.setattr(zscores, "_CBS_IDS", None)
    with db.connect() as conn:
        _drop_all_tables(conn)
    yield db
    monkeypatch.setattr(zscores, "_CBS_IDS", None)


@pytest.fixture
def conn(database):
    """A connection to the test database, committed and closed after the test."""
    with database.connect() as connection:
        yield connection

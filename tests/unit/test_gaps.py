"""Tests for error and empty-data branches in db, league, projections, prospects and utils."""

import datetime
from unittest.mock import MagicMock, patch

import pytest

import db
import league
import projections
import prospects
import utils


def test_table_exists(conn):
    conn.execute("CREATE TABLE present (id INT)")
    assert db.table_exists(conn, "present") is True
    assert db.table_exists(conn, "absent") is False


def test_projection_stats_empty_without_database_or_tables(monkeypatch, database):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    assert projections.load_stats("ZIPS", 2027, "Batter") == {}  # tables missing
    monkeypatch.delenv("DATABASE_URL")
    assert projections.load_stats("ZIPS", 2027, "Batter") == {}  # not configured


def test_projected_active_sgp_vr_sums_systems_across_player_types(conn):
    import import_lineups

    conn.execute("SET FOREIGN_KEY_CHECKS = 0")
    import_lineups.ensure_schema(conn)
    conn.executescript(
        """
        CREATE TABLE batting_projection_scores (`system` VARCHAR(16), season INT, player_id VARCHAR(32), sgp_total_above_replacement DOUBLE);
        CREATE TABLE pitching_projection_scores (`system` VARCHAR(16), season INT, player_id VARCHAR(32), sgp_total_above_replacement DOUBLE);
        INSERT INTO batting_projection_scores VALUES ('ZIPS', 2027, 'bat', 3.0);
        INSERT INTO pitching_projection_scores VALUES ('ZIPS', 2027, 'arm', 1.5)
        """
    )
    rows = [
        (1, None, "2026-09-28", 1, "Batter", "Active", "C", "bat", "Bat", "C", "TB", None, None, None, None),
        (1, None, "2026-09-28", 2, "Pitcher", "Active", "SP", "arm", "Arm", "SP", "TB", None, None, None, None),
    ]
    conn.executemany(f"INSERT INTO lineups ({', '.join(import_lineups.COLUMNS)}) VALUES ({', '.join(['%s'] * 15)})", rows)
    conn.commit()

    assert league.projected_active_sgp_vr(conn) == {"ZIPS-2027": {1: 4.5}}


def test_projected_active_sgp_vr_skips_missing_score_tables(conn):
    import import_lineups

    conn.execute("SET FOREIGN_KEY_CHECKS = 0")
    import_lineups.ensure_schema(conn)
    conn.execute(
        f"INSERT INTO lineups ({', '.join(import_lineups.COLUMNS)}) VALUES ({', '.join(['%s'] * 15)})",
        (1, None, "2026-09-28", 1, "Batter", "Active", "C", "bat", "Bat", "C", "TB", None, None, None, None),
    )
    conn.commit()
    assert league.projected_active_sgp_vr(conn) == {}


def test_active_sgp_vr_skips_missing_stat_tables(conn):
    import import_lineups

    conn.execute("SET FOREIGN_KEY_CHECKS = 0")
    import_lineups.ensure_schema(conn)
    conn.execute(
        f"INSERT INTO lineups ({', '.join(import_lineups.COLUMNS)}) VALUES ({', '.join(['%s'] * 15)})",
        (1, None, "2026-09-28", 1, "Batter", "Active", "C", "bat", "Bat", "C", "TB", None, None, None, None),
    )
    conn.commit()
    assert league.active_sgp_vr(conn) == {1: 0.0}


def test_fv_number_and_sort_value_keys():
    assert prospects.fv_number(None) is None
    assert prospects.fv_number("45+") == 45.5
    assert prospects.fv_number("N/A") is None
    p = prospects.Prospect(name="Jesús", fangraphs_rank=4, bp_rank=2, mlb_rank=9, org_rank=1, age=19.0, fv="50")
    assert [prospects.sort_value(p, key) for key in ("fangraphs", "bp", "mlb", "org", "age", "fv", "rank")] == [
        4, 2, 9, 1, 19.0, 50.0, 2,
    ]
    assert prospects.sort_value(p, "name") == "jesus"
    assert prospects.sort_value(prospects.Prospect(name="x"), "rank") is None


def test_build_prospects_merges_mlb_rows_and_ignores_unknown_sources():
    base = {c.strip("`"): None for c in prospects.COLUMNS}
    rows = [
        {**base, "source": "mlb", "player_id": "p", "rank": 7, "name": "P", "pos": "-"},
        {**base, "source": "other", "player_id": "p", "rank": 1, "name": "P", "pos": "SS", "sec_pos": "SS"},
    ]
    [p] = prospects.build_prospects(rows, "Pitcher")
    assert (p.mlb_rank, p.eligible_positions) == (7, ["SS"])


def seed_prospects(conn):
    columns = ", ".join(c.strip("`") and c for c in prospects.COLUMNS)
    definitions = ", ".join(
        f"{c} {'VARCHAR(32)' if c.strip('`') in ('source', 'list_type', 'player_id', 'name', 'team', 'pos', 'sec_pos', 'fv', 'level', 'eta', 'versa', 'tj_date', 'fb_type') else 'DOUBLE'}"
        for c in prospects.COLUMNS
    )
    conn.execute(f"CREATE TABLE rankings_prospects ({definitions}, player_type VARCHAR(16), season INT, snapshot VARCHAR(32))")
    base = {c.strip("`"): None for c in prospects.COLUMNS}
    data = [
        {"source": "fangraphs", "list_type": "a", "player_id": "p1", "rank": 1, "name": "One", "season": 2026, "snapshot": "2026-05-01T00:00:00Z"},
        {"source": "fangraphs", "list_type": "a", "player_id": "old", "rank": 1, "name": "Old", "season": 2025, "snapshot": "2025-05-01T00:00:00Z"},
        {"source": "bp", "list_type": "b", "player_id": "p2", "rank": 2, "name": "Two", "season": 2026, "snapshot": "bad"},
        {"source": "mlb", "list_type": "c", "player_id": "p1", "rank": 3, "name": "One", "season": 2026, "snapshot": "2026-05-02T00:00:00Z"},
    ]
    for entry in data:
        values = {**base, **entry, "player_type": "Batter"}
        names = list(values)
        conn.execute(
            f"INSERT INTO rankings_prospects ({', '.join(f'`{n}`' for n in names)}) VALUES ({', '.join(['%s'] * len(names))})",
            list(values.values()),
        )
    conn.commit()


def test_load_prospects_and_last_updated(conn, monkeypatch):
    seed_prospects(conn)
    monkeypatch.setattr(prospects, "fantasy_teams", lambda _conn: {"p1": (4, "Alpha")})

    found = prospects.load_prospects("Batter")

    assert [(p.name, p.fantasy_team, p.mlb_rank) for p in found] == [("One", "Alpha", 3), ("Two", None, None)]
    assert prospects.load_prospects("Pitcher") == []
    assert prospects.last_updated() == [
        ("FanGraphs", datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)),
        ("MLB", datetime.datetime(2026, 5, 2, tzinfo=datetime.timezone.utc)),
    ]  # the BP snapshot is unparseable and skipped


def test_prospects_without_database_or_tables(database, monkeypatch):
    assert prospects.load_prospects("Batter") == []
    assert prospects.last_updated() == []
    monkeypatch.delenv("DATABASE_URL")
    assert prospects.load_prospects("Batter") == []
    assert prospects.last_updated() == []


def test_profiles_are_cached_until_the_ttl_expires(monkeypatch):
    monkeypatch.setattr(prospects, "_PROFILES", None)
    calls = []

    def load(kind):
        calls.append(kind)
        return [prospects.Prospect(name=kind, player_id=kind.lower()), prospects.Prospect(name="no id")]

    monkeypatch.setattr(prospects, "load_prospects", load)
    assert set(prospects.profiles()) == {"batter", "pitcher"}
    prospects.profiles()
    assert calls == ["Batter", "Pitcher"]
    monkeypatch.setattr(prospects.time, "monotonic", lambda: 10**9)
    prospects.profiles()
    assert len(calls) == 4


def test_latest_activation_is_none_without_a_matching_transaction():
    response = MagicMock()
    response.json.return_value = {"people": [{"transactions": [{"date": "2026-01-01", "description": "Traded"}]}]}
    with patch("utils.requests.get", return_value=response):
        assert utils.get_mlb_latest_activation({"id": 1}) == (None, None)


def test_prospects_imports_as_a_package_module():
    """Importing ``src.prospects`` takes the relative-import path used when run as a package."""
    import importlib
    import sys
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[2])
    sys.path.insert(0, root)
    try:
        module = importlib.import_module("src.prospects")
        assert module.fv_number("50") == 50.0
    finally:
        sys.path.remove(root)
        for name in [n for n in sys.modules if n == "src" or n.startswith("src.")]:
            del sys.modules[name]


import pytest

import db
import zscores


def test_database_url_comes_from_the_environment_and_defaults_to_the_pymysql_driver(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://app:secret@db.example:3307/rummy")
    url = db.database_url()
    assert (url.drivername, url.host, url.port, url.database) == ("mysql+pymysql", "db.example", 3307, "rummy")
    assert url.query["charset"] == "utf8mb4"


def test_database_url_is_required(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(db.NotConfigured):
        db.connect()
    assert db.connect_or_none() is None


def test_rows_are_read_by_position_or_column_name(conn):
    row = conn.execute("SELECT 1 AS a, 'x' AS b").fetchone()
    assert (row[0], row["b"], row.keys(), tuple(row)) == (1, "x", ["a", "b"], (1, "x"))
    assert dict(row) == {"a": 1, "b": "x"}
    with pytest.raises(KeyError):
        row["missing"]


def test_a_missing_table_is_a_schema_error(conn):
    with pytest.raises(db.SchemaError):
        conn.execute("SELECT * FROM nope")


def test_write_frame_replaces_the_table_with_indexable_text_columns(conn):
    import pandas as pd

    conn.write_frame(pd.DataFrame({"player_id": ["a"], "rank": [1]}), "t")
    conn.write_frame(pd.DataFrame({"player_id": ["b"], "rank": [2]}), "t")
    conn.execute("CREATE INDEX idx_t_player ON t (player_id)")
    assert [tuple(row) for row in conn.execute("SELECT player_id, `rank` FROM t").fetchall()] == [("b", 2)]


def test_free_agents_can_be_filtered_to_a_whole_position(conn, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    columns = ["z_HR", "z_HR_rank", "z_total", "z_total_rank"]
    conn.execute(
        "CREATE TABLE batting (name VARCHAR(64), player_id VARCHAR(32), positions VARCHAR(32), team VARCHAR(8),"
        " team_id INT, AB INT, HR INT, `rank` INT, "
        + ", ".join(f"{c} DOUBLE" for c in columns) + ", sgp_total_above_replacement_rank DOUBLE)"
    )
    conn.executemany(
        "INSERT INTO batting VALUES (%s, %s, %s, 'NYY', NULL, 100, 10, 5, 1.0, 1, 1.0, 1, %s)",
        [("Catcher", "c", "C", 2), ("Corner", "k", "1B, C", 1), ("First", "f", "1B", 3), ("Shortstop", "s", "SS", 4)],
    )
    conn.commit()

    assert [p.name for p in zscores.load_free_agents("Batter", position="C")] == ["Corner", "Catcher"]
    assert [p.name for p in zscores.load_free_agents("Batter", position="1B")] == ["Corner", "First"]
    assert [p.name for p in zscores.load_free_agents("Batter", offset=1, limit=2)] == ["Catcher", "First"]
    assert zscores.load_free_agents("Batter", position="S") == []


def test_free_agents_sort_across_every_player_not_just_one_page(conn, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    columns = ["z_HR", "z_HR_rank", "z_total", "z_total_rank"]
    conn.execute(
        "CREATE TABLE batting (name VARCHAR(64), player_id VARCHAR(32), positions VARCHAR(32), team VARCHAR(8),"
        " team_id INT, AB INT, HR INT, `rank` INT, "
        + ", ".join(f"{c} DOUBLE" for c in columns) + ", sgp_total_above_replacement_rank DOUBLE)"
    )
    conn.executemany(
        "INSERT INTO batting VALUES (%s, %s, 'C', 'NYY', NULL, 100, %s, %s, 1.0, 1, 1.0, 1, %s)",
        [("Ann", "a", 10, 30, 1), ("Bob", "b", 30, 10, 2), ("Cy", "c", 20, 20, 3)],
    )
    conn.commit()
    monkeypatch.setattr(zscores, "bp_ranks", lambda: {"a": (2, None), "c": (1, 3)})

    def names(**kwargs):
        return [p.name for p in zscores.load_free_agents("Batter", **kwargs)]

    assert names(sort="stat:HR") == ["Bob", "Cy", "Ann"]
    assert names(sort="stat:HR", direction="asc") == ["Ann", "Cy", "Bob"]
    assert names(sort="cbs") == ["Bob", "Cy", "Ann"]
    assert names(sort="name", direction="desc") == ["Cy", "Bob", "Ann"]
    assert names(sort="bp") == ["Cy", "Ann", "Bob"]
    assert names(sort="bp", direction="desc", offset=1, limit=1) == ["Ann"]
    with pytest.raises(ValueError):
        zscores.free_agent_sort("Batter", "stat:XX", None)

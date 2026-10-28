import sqlite3

import league
import zscores


def make_db(path, with_standings=True):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE managers (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
        CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, abbrev TEXT, manager_id INTEGER);
        INSERT INTO managers VALUES (1, 'Ann', NULL), (2, 'Bob', NULL), (3, 'Cy', NULL);
        INSERT INTO teams VALUES (1, 'Alpha', 'A', 1), (2, 'Beta', 'B', 2), (3, 'Gamma', 'G', 3);
        """
    )
    if with_standings:
        conn.executescript(
            """
            CREATE TABLE standings (season_year, as_of_date, team_id, rank, total_points);
            INSERT INTO standings VALUES (2025, '2025-12-31', 1, 1, 100), (2025, '2025-12-31', 2, 2, 90);
            INSERT INTO standings VALUES (2026, '2026-12-31', 2, 1, 120), (2026, '2026-12-31', 1, 2, 110);
            """
        )
    conn.commit()
    conn.close()


def test_league_teams_follow_the_most_recent_standings_and_list_unranked_teams_last(tmp_path):
    db = tmp_path / "t.db"
    make_db(db)

    snapshot, teams = league.league_teams(db)

    assert snapshot == {"season_year": 2026, "as_of_date": "2026-12-31"}
    assert [(t["name"], t["rank"], t["manager"]) for t in teams] == [
        ("Beta", 1, "Bob"), ("Alpha", 2, "Ann"), ("Gamma", None, "Cy"),
    ]


def test_league_teams_without_standings_are_alphabetical(tmp_path):
    db = tmp_path / "t.db"
    make_db(db, with_standings=False)

    snapshot, teams = league.league_teams(db)

    assert snapshot is None
    assert [t["name"] for t in teams] == ["Alpha", "Beta", "Gamma"]


def test_league_teams_handles_a_missing_database(tmp_path):
    assert league.league_teams(tmp_path / "missing.db") == (None, [])


def test_get_team_returns_one_team_or_none(tmp_path):
    db = tmp_path / "t.db"
    make_db(db)

    assert league.get_team(2, db)["name"] == "Beta"
    assert league.get_team(99, db) is None


def test_load_players_returns_a_teams_players_or_the_free_agents_best_first(tmp_path, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    monkeypatch.setenv("PITCHING_SCORING_CATEGORIES", "INN")
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE batting (name, positions, team, team_id, AB, HR, z_HR, z_total,"
        " z_HR_rank, z_total_rank, sgp_total_above_replacement, sgp_total_above_replacement_rank,"
        " z_HR_above_replacement, z_total_above_replacement, z_HR_above_replacement_rank,"
        " z_total_above_replacement_rank, sgp_HR_above_replacement, sgp_HR_above_replacement_rank,"
        " sgp_HR, sgp_total, sgp_HR_rank, sgp_total_rank)"
    )
    conn.executemany(
        "INSERT INTO batting VALUES (?, ?, 'NYY', ?, ?, ?, 1.0, 1.0, 1, 1, ?, ?,"
        " 1.0, 1.0, 1, 1, 1.0, 1, 1.0, 1.0, 1, 1)",
        [
            ("Owned Low", "C,1B", 7, 100, 5, -1.0, 9),
            ("Owned High", "SS", 7, 100, 30, 4.0, 2),
            ("Free Agent", "LF", None, 100, 12, 1.0, 5),
            ("Free Agent No AB", "LF", None, 0, 0, None, None),
            ("Other Team", "RF", 8, 100, 20, 2.0, 3),
        ],
    )
    conn.commit()
    conn.close()

    team = zscores.load_players(7, db)
    assert [p.name for p in team["Batter"]] == ["Owned High", "Owned Low"]
    assert team["Batter"][1].eligible_positions == ["C", "1B"]
    assert team["Batter"][0].scoring_stats == {"HR": 30}
    assert team["Pitcher"] == []

    free = zscores.load_players(None, db, limit=5)
    assert [p.name for p in free["Batter"]] == ["Free Agent"]

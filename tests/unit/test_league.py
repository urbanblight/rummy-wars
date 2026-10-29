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
        "CREATE TABLE batting (name, player_id, positions, team, team_id, AB, HR, z_HR, z_total, rank,"
        " z_HR_rank, z_total_rank, sgp_total_above_replacement, sgp_total_above_replacement_rank,"
        " z_HR_above_replacement, z_total_above_replacement, z_HR_above_replacement_rank,"
        " z_total_above_replacement_rank, sgp_HR_above_replacement, sgp_HR_above_replacement_rank,"
        " sgp_HR, sgp_total, sgp_HR_rank, sgp_total_rank)"
    )
    conn.executemany(
        "INSERT INTO batting VALUES (?, ?, ?, 'NYY', ?, ?, ?, 1.0, 1.0, ?, 1, 1, ?, ?,"
        " 1.0, 1.0, 1, 1, 1.0, 1, 1.0, 1.0, 1, 1)",
        [
            ("Owned Low", "owned low", "C,1B", 7, 100, 5, 9999, -1.0, 9),
            ("Owned High", "owned high", "SS", 7, 100, 30, 12, 4.0, 2),
            ("Free Agent", "free agent", "LF", None, 100, 12, 80, 1.0, 5),
            ("Free Agent No AB", "free agent no ab", "LF", None, 0, 0, 300, None, None),
            ("Other Team", "other team", "RF", 8, 100, 20, 40, 2.0, 3),
        ],
    )
    conn.commit()
    conn.close()

    team = zscores.load_players(7, db)
    assert [p.name for p in team["Batter"]] == ["Owned High", "Owned Low"]
    assert team["Batter"][1].eligible_positions == ["C", "1B"]
    assert team["Batter"][0].scoring_stats == {"HR": 30}
    assert [p.cbs_rank for p in team["Batter"]] == [12, None]
    assert team["Pitcher"] == []

    free = zscores.load_players(None, db, limit=5)
    assert [p.name for p in free["Batter"]] == ["Free Agent"]


def test_load_team_roster_follows_the_latest_lineup_order_and_attaches_scores(tmp_path, monkeypatch):
    import import_lineups  # noqa: E402

    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    monkeypatch.setenv("PITCHING_SCORING_CATEGORIES", "INN")
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    import_lineups.ensure_schema(conn)
    scored = ["HR", "total"]
    columns = ["name", "player_id", "positions", "team", "team_id", "AB", "HR", "rank"]
    for kind in ("z", "sgp"):
        for name in scored:
            columns += [f"{kind}_{name}", f"{kind}_{name}_rank", f"{kind}_{name}_above_replacement", f"{kind}_{name}_above_replacement_rank"]
    conn.execute(f"CREATE TABLE batting ({', '.join(columns)})")
    for name, norm, hr, vr, vr_rank in (("Bench Bat", "bench bat", 3, -2.0, 9), ("Slot Bat", "slot bat", 30, 4.0, 1)):
        values = {"name": name, "player_id": norm, "positions": "1B", "team": "TB", "team_id": 7, "AB": 100, "HR": hr, "rank": 5}
        for kind in ("z", "sgp"):
            for stat in scored:
                values.update({
                    f"{kind}_{stat}": 1.0, f"{kind}_{stat}_rank": 1,
                    f"{kind}_{stat}_above_replacement": vr, f"{kind}_{stat}_above_replacement_rank": vr_rank,
                })
        conn.execute(f"INSERT INTO batting ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})", list(values.values()))
    lineup = [
        (7, None, "2026-06-01", 1, "Batter", "Active", "C", "old bat", "Old Bat", "C", "TB", None, None, None, None),
        (7, None, "2026-09-28", 1, "Batter", "Active", "DH", "slot bat", "Slot Bat", "1B,LF", "TB", None, None, None, None),
        (7, None, "2026-09-28", 2, "Batter", "Reserves", "1B", "bench bat", "Bench Bat", "1B", "TB", None, None, None, None),
        (7, None, "2026-09-28", 3, "Batter", "Minors", "SS", "no stats kid", "No Stats Kid", "SS", "TB", None, None, None, None),
    ]
    conn.executemany(f"INSERT INTO lineups VALUES ({', '.join('?' * 15)})", lineup)
    conn.commit()
    conn.close()

    info, players = zscores.load_team_roster(7, db)

    assert info == {"effective_date": "2026-09-28", "period_id": None}
    assert [(p.name, p.roster_slot, p.status) for p in players["Batter"]] == [
        ("Slot Bat", "DH", "Active"), ("Bench Bat", "1B", "Reserves"), ("No Stats Kid", "SS", "Minors"),
    ]
    assert players["Batter"][0].eligible_positions == ["1B", "LF"]
    assert players["Batter"][0].sgp_above_replacement["total"] == (4.0, 1)
    assert players["Batter"][2].scoring_stats == {}  # not in the stats tables: listed without scores
    assert players["Pitcher"] == []


def test_load_team_roster_falls_back_to_best_first_without_a_lineup(tmp_path, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    monkeypatch.setenv("PITCHING_SCORING_CATEGORIES", "INN")
    info, players = zscores.load_team_roster(7, tmp_path / "missing.db")
    assert info is None
    assert players == {"Batter": [], "Pitcher": []}


def test_active_sgp_vr_sums_only_the_players_active_in_each_teams_latest_lineup(tmp_path):
    import import_lineups  # noqa: E402

    db = tmp_path / "t.db"
    make_db(db)
    conn = sqlite3.connect(db)
    import_lineups.ensure_schema(conn)
    conn.executescript(
        """
        CREATE TABLE batting (team_id, player_id, sgp_total_above_replacement);
        CREATE TABLE pitching (team_id, player_id, sgp_total_above_replacement);
        INSERT INTO batting VALUES (1, 'old bat', 100.0), (1, 'bat a', 5.0), (1, 'bench bat', 50.0), (2, 'bat b', -3.0), (2, 'unscored', NULL);
        INSERT INTO pitching VALUES (1, 'arm a', 2.5), (2, 'arm b', 1.0);
        """
    )
    rows = [
        (1, None, "2026-06-01", 1, "Batter", "Active", "C", "old bat", "Old Bat", "C", "TB", None, None, None, None),
        (1, None, "2026-09-28", 1, "Batter", "Active", "C", "bat a", "Bat A", "C", "TB", None, None, None, None),
        (1, None, "2026-09-28", 2, "Batter", "Reserves", "1B", "bench bat", "Bench Bat", "1B", "TB", None, None, None, None),
        (1, None, "2026-09-28", 3, "Pitcher", "Active", "SP", "arm a", "Arm A", "SP", "TB", None, None, None, None),
        (2, None, "2026-09-28", 1, "Batter", "Active", "C", "bat b", "Bat B", "C", "TB", None, None, None, None),
        (2, None, "2026-09-28", 2, "Batter", "Active", "1B", "unscored", "Unscored", "1B", "TB", None, None, None, None),
        (2, None, "2026-09-28", 3, "Pitcher", "Active", "RP", "arm b", "Arm B", "RP", "TB", None, None, None, None),
    ]
    conn.executemany(f"INSERT INTO lineups VALUES ({', '.join('?' * 15)})", rows)
    conn.commit()

    assert league.active_sgp_vr(conn) == {1: 7.5, 2: -2.0}
    conn.close()

    _snapshot, teams = league.league_teams(db)
    assert {t["name"]: t["active_sgp_vr"] for t in teams} == {"Alpha": 7.5, "Beta": -2.0, "Gamma": None}


def test_active_sgp_vr_is_empty_without_lineups(tmp_path):
    db = tmp_path / "t.db"
    make_db(db)
    assert all(team["active_sgp_vr"] is None for team in league.league_teams(db)[1])

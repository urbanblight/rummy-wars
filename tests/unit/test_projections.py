import db
import models
import projections


def make_db(conn):
    conn.executescript(
        """
        CREATE TABLE batting_projections (`system` VARCHAR(32), season INT, snapshot VARCHAR(32),
            player_id VARCHAR(32), name VARCHAR(64), hr INT, avg DOUBLE, rbi INT);
        CREATE TABLE pitching_projections (`system` VARCHAR(32), season INT, snapshot VARCHAR(32),
            player_id VARCHAR(32), name VARCHAR(64), w INT, qs INT, ip DOUBLE, so INT, sv INT);
        INSERT INTO batting_projections VALUES
            ('ZIPS', 2027, '2026-10-01', 'a1', 'Old', 1, 0.1, 1),
            ('ZIPS', 2027, '2026-10-02', 'a1', 'Slugger', 38, 0.28, 105),
            ('ZIPS', 2027, '2026-10-02', NULL, 'Unknown', 5, 0.2, 5);
        INSERT INTO pitching_projections VALUES ('Steamer', 2026, '2026-10-02', 'p1', 'Arm', 10, 12, 180.0, 200, 0)
        """
    )
    conn.commit()


def test_systems_lists_each_system_and_season_newest_first(conn):
    assert projections.systems() == []
    make_db(conn)
    assert [(s["key"], s["label"]) for s in projections.systems()] == [
        ("ZIPS-2027", "ZIPS 2027"), ("Steamer-2026", "Steamer 2026"),
    ]


def test_apply_stats_uses_the_latest_snapshot_and_maps_scoring_categories(conn, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "BA,HR,RBI,SB")
    monkeypatch.setenv("PITCHING_SCORING_CATEGORIES", "INN,W+QS,Kd9,S")
    make_db(conn)
    batter = models.Player("Slugger", "", [], "NYY", "", "Batter", player_id="a1", scoring_stats={"HR": 1})
    nobody = models.Player("Nobody", "", [], "NYY", "", "Batter")
    arm = models.Player("Arm", "", [], "BOS", "", "Pitcher", player_id="p1")

    projections.apply_stats([batter, nobody], "Batter", "ZIPS", 2027)
    projections.apply_stats([arm], "Pitcher", "Steamer", 2026)

    assert batter.scoring_stats == {"BA": 0.28, "HR": 38, "RBI": 105, "SB": None}  # SB column not stored
    assert nobody.scoring_stats == {}
    assert arm.scoring_stats == {"INN": 180.0, "W+QS": 22, "Kd9": 10.0, "S": 0}


def test_scores_are_calculated_for_projections_and_shown_without_a_cbs_rank(conn, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR,RBI")
    monkeypatch.setenv("PITCHING_SCORING_CATEGORIES", "")
    conn.executescript(
        """
        CREATE TABLE batting (AB INT, HR INT, RBI INT, `rank` INT, name VARCHAR(64), positions VARCHAR(32),
            team VARCHAR(8), player_id VARCHAR(32), team_id INT);
        INSERT INTO batting VALUES
            (500, 10, 50, 1, 'A', 'C', 'NYY', 'a', NULL),
            (500, 20, 60, 2, 'B', 'C', 'NYY', 'b', NULL),
            (500, 30, 90, 3, 'C', 'C', 'NYY', 'c', 1);
        CREATE TABLE batting_projections (`system` VARCHAR(32), season INT, snapshot VARCHAR(32),
            player_id VARCHAR(32), name VARCHAR(64), ab INT, hr INT, rbi INT, war DOUBLE);
        INSERT INTO batting_projections VALUES
            ('ZIPS', 2027, 's', 'a', 'A', 500, 12, 55, 1.0),
            ('ZIPS', 2027, 's', 'b', 'B', 500, 22, 70, 2.0),
            ('ZIPS', 2027, 's', 'c', 'C', 500, 35, 100, 4.0)
        """
    )
    conn.commit()

    projections.save_projection_scores()

    best = models.Player("C", "", [], "NYY", "", "Batter", player_id="c", cbs_rank=3)
    projections.apply_stats([best], "Batter", "ZIPS", 2027)
    assert best.cbs_rank is None
    assert best.z_total > 0 and best.z_total_rank == 1
    assert best.z_above_replacement["total"][0] > 0
    assert best.scoring_stats["HR"] == 35


def test_scores_are_skipped_for_a_player_type_whose_tables_are_missing(conn, monkeypatch):
    # Set the categories explicitly: CI has no ``.env``, so nothing else would get past the "no categories" skip.
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    monkeypatch.setenv("PITCHING_SCORING_CATEGORIES", "")

    projections.save_projection_scores()

    assert not db.table_exists(conn, "batting_projection_scores")

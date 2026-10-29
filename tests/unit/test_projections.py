import sqlite3

import models
import projections


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE batting_projections (system, season, snapshot, player_id, name, hr, avg, rbi);
        CREATE TABLE pitching_projections (system, season, snapshot, player_id, name, w, qs, ip, so, sv);
        INSERT INTO batting_projections VALUES
            ('ZIPS', 2027, '2026-10-01', 'a1', 'Old', 1, 0.1, 1),
            ('ZIPS', 2027, '2026-10-02', 'a1', 'Slugger', 38, 0.28, 105),
            ('ZIPS', 2027, '2026-10-02', NULL, 'Unknown', 5, 0.2, 5);
        INSERT INTO pitching_projections VALUES ('Steamer', 2026, '2026-10-02', 'p1', 'Arm', 10, 12, 180.0, 200, 0);
        """
    )
    conn.commit()
    conn.close()


def test_systems_lists_each_system_and_season_newest_first(tmp_path):
    db = tmp_path / "t.db"
    make_db(db)
    assert [(s["key"], s["label"]) for s in projections.systems(db)] == [
        ("ZIPS-2027", "ZIPS 2027"), ("Steamer-2026", "Steamer 2026"),
    ]
    assert projections.systems(tmp_path / "missing.db") == []


def test_apply_stats_uses_the_latest_snapshot_and_maps_scoring_categories(tmp_path, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "BA,HR,RBI,SB")
    monkeypatch.setenv("PITCHING_SCORING_CATEGORIES", "INN,W+QS,Kd9,S")
    db = tmp_path / "t.db"
    make_db(db)
    batter = models.Player("Slugger", "", [], "NYY", "", "Batter", player_id="a1", scoring_stats={"HR": 1})
    nobody = models.Player("Nobody", "", [], "NYY", "", "Batter")
    arm = models.Player("Arm", "", [], "BOS", "", "Pitcher", player_id="p1")

    projections.apply_stats([batter, nobody], "Batter", "ZIPS", 2027, db)
    projections.apply_stats([arm], "Pitcher", "Steamer", 2026, db)

    assert batter.scoring_stats == {"BA": 0.28, "HR": 38, "RBI": 105, "SB": None}  # SB column not stored
    assert nobody.scoring_stats == {}
    assert arm.scoring_stats == {"INN": 180.0, "W+QS": 22, "Kd9": 10.0, "S": 0}


def test_scores_are_calculated_for_projections_and_shown_without_a_cbs_rank(tmp_path, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR,RBI")
    monkeypatch.setenv("PITCHING_SCORING_CATEGORIES", "")
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE batting (AB, HR, RBI, rank, name, positions, team, player_id, team_id);
        INSERT INTO batting VALUES
            (500, 10, 50, 1, 'A', 'C', 'NYY', 'a', NULL),
            (500, 20, 60, 2, 'B', 'C', 'NYY', 'b', NULL),
            (500, 30, 90, 3, 'C', 'C', 'NYY', 'c', 1);
        CREATE TABLE batting_projections (system, season, snapshot, player_id, name, ab, hr, rbi, war);
        INSERT INTO batting_projections VALUES
            ('ZIPS', 2027, 's', 'a', 'A', 500, 12, 55, 1.0),
            ('ZIPS', 2027, 's', 'b', 'B', 500, 22, 70, 2.0),
            ('ZIPS', 2027, 's', 'c', 'C', 500, 35, 100, 4.0);
        """
    )
    conn.commit()
    conn.close()

    projections.save_projection_scores(db)

    best = models.Player("C", "", [], "NYY", "", "Batter", player_id="c", cbs_rank=3)
    projections.apply_stats([best], "Batter", "ZIPS", 2027, db)
    assert best.cbs_rank is None
    assert best.z_total > 0 and best.z_total_rank == 1
    assert best.z_above_replacement["total"][0] > 0
    assert best.scoring_stats["HR"] == 35

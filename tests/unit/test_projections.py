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

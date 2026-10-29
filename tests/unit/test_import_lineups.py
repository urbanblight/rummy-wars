import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_lineups  # noqa: E402
import import_players  # noqa: E402

LINEUP = '''Batters
,Pos,Players, 0/0- 0/0,Proj,Actual,Rost,Start,BA,R,HR,RBI,SB,
,C,"Liam Hicks C,1B | TB ","Home: 0
Away: 0 ",452,102,94%,77%,0.284,66,18,82,3
,DH,Michael Harris II CF | ATL ,"Home: 0
Away: 0 ",116,29,99%,92%,0.294,79,26,87,12
,,,,,,,,0.289,145,44,169,15
Reserves
, DH,Garrett Mitchell CF | MIL ,"Home: 0
Away: 0 ",310,132,38%,27%,0.263,74,11,65,14
Injured
Minors
,SS,Caleb Bonemer SS | CHW ,"Home: 0
Away: 0 ",N/A,N/A,8%,0%,0.000,0,0,0,0
Pitchers
,Pos,Players, 0/0- 0/0,Proj,Actual,Rost,Start,ERA,WHIP,W,K,S,
,SP,Bubba Chandler SP | PIT ,"Home: 0
Away: 0 ",505,475,69%,38%,4.62,1.40,8,140,0
,RP,"Jacob Latz SP,RP | TEX ","Home: 0
Away: 0 ",411,121,86%,76%,2.12,0.81,3,83,30
Reserves
,RP,Rico Garcia RP | BAL ,"Home: 0
Away: 0 ",725,294,23%,19%,3.09,0.97,5,68,10
Active: 20 Reserve: 7 Injured: 8 Minors: 20
'''


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO teams VALUES (?, ?)", [(7, "The Outfield"), (8, "Wheatus")])
    conn.executescript(import_players.SCHEMA)
    conn.execute(
        "INSERT INTO players (player_id, name, cbs_name, team, pos, active) VALUES "
        "('hicksli01', 'Liam Hicks', 'Liam Hicks', 'TB', 'C', 1), "
        "('harrimi04', 'Michael Harris II', 'Michael Harris', 'ATL', 'OF', 1)"
    )
    import_lineups.ensure_schema(conn)
    return conn


def test_parse_filename_reads_offseason_and_inseason_names():
    assert import_lineups.parse_filename("roster-overview-7-20260928.csv") == import_lineups.LineupFile(7, None, "2026-09-28")
    assert import_lineups.parse_filename("roster-overview-16-12-20260928.csv") == import_lineups.LineupFile(16, 12, "2026-09-28")


@pytest.mark.parametrize("name", [
    "roster-overview-7.csv", "roster-7-20260928.csv", "roster-overview-7-20260928.txt", "roster-overview-7-20261340.csv",
])
def test_parse_filename_rejects_other_names_and_impossible_dates(name):
    with pytest.raises(ValueError):
        import_lineups.parse_filename(name)


def test_parse_lineup_reads_every_player_with_type_status_and_slot():
    players = import_lineups.parse_lineup(LINEUP)

    assert [(p["player_type"], p["status"], p["slot"], p["name"]) for p in players] == [
        ("Batter", "Active", "C", "Liam Hicks"),
        ("Batter", "Active", "DH", "Michael Harris II"),
        ("Batter", "Reserves", "DH", "Garrett Mitchell"),
        ("Batter", "Minors", "SS", "Caleb Bonemer"),
        ("Pitcher", "Active", "SP", "Bubba Chandler"),
        ("Pitcher", "Active", "RP", "Jacob Latz"),
        ("Pitcher", "Reserves", "RP", "Rico Garcia"),
    ]
    assert [p["slot_order"] for p in players] == list(range(1, 8))
    hicks = players[0]
    assert (hicks["positions"], hicks["mlb_team"]) == ("C,1B", "TB")
    assert (hicks["proj_rank"], hicks["actual_rank"], hicks["rostered_pct"], hicks["started_pct"]) == (452, 102, 94.0, 77.0)
    assert players[5]["positions"] == "SP,RP"


def test_parse_lineup_treats_missing_ranks_as_null():
    bonemer = import_lineups.parse_lineup(LINEUP)[3]
    assert (bonemer["proj_rank"], bonemer["actual_rank"]) == (None, None)
    assert bonemer["rostered_pct"] == 8.0


def test_parse_lineup_rejects_empty_or_unreadable_files():
    with pytest.raises(ValueError):
        import_lineups.parse_lineup("Batters\n,Pos,Players\n")
    with pytest.raises(ValueError):
        import_lineups.parse_lineup("Batters\n,C,not a player cell,x,1,2,3%,4%\n")


def test_import_lineup_replaces_the_same_lineup_and_keeps_others(tmp_path):
    conn = make_db(tmp_path / "t.db")
    offseason = tmp_path / "roster-overview-7-20260928.csv"
    inseason = tmp_path / "roster-overview-7-12-20260928.csv"
    other_team = tmp_path / "roster-overview-8-20260928.csv"
    for path in (offseason, inseason, other_team):
        path.write_text(LINEUP)

    for path in (offseason, inseason, other_team, offseason):
        import_lineups.import_lineup(path, conn)

    rows = conn.execute("SELECT team_id, period_id, COUNT(*) FROM lineups GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
    assert rows == [(7, None, 7), (7, 12, 7), (8, None, 7)]


def test_import_lineup_stores_canonical_player_ids_and_null_for_unknown_players(tmp_path):
    conn = make_db(tmp_path / "t.db")
    path = tmp_path / "roster-overview-7-20260928.csv"
    path.write_text(LINEUP)

    import_lineups.import_lineup(path, conn)

    ids = dict(conn.execute("SELECT name, player_id FROM lineups"))
    assert ids["Liam Hicks"] == "hicksli01"
    assert ids["Michael Harris II"] == "harrimi04"  # CBS adds the suffix the map drops
    assert ids["Bubba Chandler"] is None


def test_ensure_schema_rebuilds_a_lineups_table_without_player_ids():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE lineups (norm_name TEXT)")
    import_lineups.ensure_schema(conn)
    assert "player_id" in [row[1] for row in conn.execute("PRAGMA table_info(lineups)")]


def test_import_lineup_needs_the_players_table(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT)")
    path = tmp_path / "roster-overview-7-20260928.csv"
    path.write_text(LINEUP)
    with pytest.raises(RuntimeError, match="import_players"):
        import_lineups.import_lineup(path, conn)


def test_import_lineup_requires_a_known_team(tmp_path):
    conn = make_db(tmp_path / "t.db")
    path = tmp_path / "roster-overview-99-20260928.csv"
    path.write_text(LINEUP)

    with pytest.raises(ValueError, match="no team with id 99"):
        import_lineups.import_lineup(path, conn)


def test_find_files_expands_folders_and_skips_other_csvs(tmp_path):
    (tmp_path / "roster-overview-7-20260928.csv").write_text("")
    (tmp_path / "notes.csv").write_text("")

    files, skipped = import_lineups.find_files([tmp_path])

    assert [f.name for f in files] == ["roster-overview-7-20260928.csv"]
    assert [f.name for f in skipped] == ["notes.csv"]


def test_main_imports_a_folder_and_reports_errors(tmp_path, capsys):
    db = tmp_path / "t.db"
    make_db(db).commit()
    folder = tmp_path / "lineups"
    folder.mkdir()
    (folder / "roster-overview-7-20260928.csv").write_text(LINEUP)

    assert import_lineups.main([str(folder), "--db", str(db)]) == 0
    assert "Team 7 (offseason, 2026-09-28): 7 players" in capsys.readouterr().out

    (folder / "roster-overview-99-20260928.csv").write_text(LINEUP)
    assert import_lineups.main([str(folder), "--db", str(db)]) == 1
    assert "no team with id 99" in capsys.readouterr().err

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import import_players  # noqa: E402
import import_projections  # noqa: E402

CSV = (
    "\ufeffName,Team,G,PA,HR,1B,AVG,wRC+,WAR,PlayerId,MLBAMID\n"
    '"Aaron Judge","NYY",133,584,38,70,0.280255,169.7,4.7,"15640",592450\n'
    '"Nobody Known","BOS",10,20,,1,0.2,50,,"zz1",1\n'
)


def make_db(conn):
    conn.executescript(import_players.SCHEMA)
    conn.execute("INSERT INTO players (player_id, name, fangraphs_id, mlb_id) VALUES ('judgeaa01', 'Aaron Judge', '15640', '592450')")
    conn.executescript(import_projections.SCHEMA)


@pytest.mark.parametrize("name,expected", [
    ("ZIPS-2027-batting.csv", ("ZIPS", 2027, "batting")),
    ("ZIPS-2027.pitching.csv", ("ZIPS", 2027, "pitching")),
    ("Steamer-RoS-2026-batting.csv", ("Steamer-RoS", 2026, "batting")),
])
def test_parse_filename(name, expected):
    assert import_projections.parse_filename(name) == expected


def test_parse_filename_rejects_other_names():
    with pytest.raises(ValueError):
        import_projections.parse_filename("ZIPS-2027.csv")


def test_import_projection_stores_player_ids_stats_and_a_new_snapshot_each_time(tmp_path, conn):
    make_db(conn)
    path = tmp_path / "ZIPS-2027-batting.csv"
    path.write_text(CSV, encoding="utf-8")

    assert import_projections.import_projections(path, conn, "2026-10-29T00:00:00Z") == ("ZIPS", 2027, "batting", 2)
    import_projections.import_projections(path, conn, "2026-10-30T00:00:00Z")

    rows = conn.execute(
        "SELECT snapshot, player_id, name, pa, hr, singles, avg, wrc_plus, war FROM batting_projections "
        "WHERE snapshot = '2026-10-29T00:00:00Z' ORDER BY name"
    ).fetchall()
    assert rows == [
        ("2026-10-29T00:00:00Z", "judgeaa01", "Aaron Judge", 584, 38, 70, 0.280255, 169.7, 4.7),
        ("2026-10-29T00:00:00Z", None, "Nobody Known", 20, None, 1, 0.2, 50.0, None),
    ]
    assert conn.execute("SELECT COUNT(DISTINCT snapshot) FROM batting_projections").fetchone() == (2,)
    assert conn.execute("SELECT COUNT(*) FROM pitching_projections").fetchone() == (0,)


def test_main_imports_a_folder_and_needs_the_players_table(tmp_path, conn, capsys):
    folder = tmp_path / "projections"
    folder.mkdir()
    (folder / "ZIPS-2027-batting.csv").write_text(CSV, encoding="utf-8")
    (folder / "notes.csv").write_text("")

    assert import_projections.main([str(folder)]) == 1
    assert capsys.readouterr().err

    make_db(conn)
    conn.commit()
    assert import_projections.main([str(folder)]) == 0
    assert "ZIPS 2027 batting: 2 players" in capsys.readouterr().out


def test_a_file_of_the_wrong_type_is_rejected_without_inserting_rows(tmp_path, conn):
    make_db(conn)
    path = tmp_path / "ZIPS-2027-pitching.csv"
    path.write_text(CSV, encoding="utf-8")  # batting columns

    with pytest.raises(ValueError, match="does not look like a pitching export"):
        import_projections.import_projections(path, conn, "2026-10-29T00:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM pitching_projections").fetchone() == (0,)

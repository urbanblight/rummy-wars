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


PITCHING_CSV = "Name,Team,W,IP,ERA,SO,PlayerId,MLBAMID\nAce Pitcher,NYY,10,180.1,3.1,200,p1,\n"


def test_widen_count_columns_converts_int_columns_to_double(conn):
    make_db(conn)
    conn.execute("ALTER TABLE batting_projections MODIFY pa INT")
    import_projections.widen_count_columns(conn)
    data_type = conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'batting_projections' AND column_name = 'pa'"
    ).fetchone()[0]
    assert data_type == "double"


def test_find_files_keeps_explicit_paths_and_skips_other_csvs(tmp_path):
    explicit = tmp_path / "whatever.csv"
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "ZIPS-2027-batting.csv").write_text("")
    (tmp_path / "x" / "notes.csv").write_text("")

    files, skipped = import_projections.find_files([explicit, tmp_path / "x"])

    assert [f.name for f in files] == ["whatever.csv", "ZIPS-2027-batting.csv"]
    assert [f.name for f in skipped] == ["notes.csv"]


def test_load_player_ids_needs_the_players_table(conn):
    with pytest.raises(RuntimeError, match="import_players"):
        import_projections.load_player_ids(conn)


def test_load_player_ids_indexes_both_id_kinds(conn):
    make_db(conn)
    by_fangraphs, by_mlb = import_projections.load_player_ids(conn)
    assert by_fangraphs == {"15640": ["judgeaa01"]}
    assert by_mlb == {"592450": ["judgeaa01"]}


def test_pick_disambiguates_two_way_players():
    pick = import_projections.pick
    assert pick(None, "batting") is None
    assert pick(["a"], "batting") == "a"
    assert pick(["ohtani", "ohtanip"], "batting") == "ohtani"
    assert pick(["ohtani", "ohtanip"], "pitching") == "ohtanip"
    assert pick(["a", "b"], "batting") is None  # genuinely ambiguous


def test_id_text_normalises_floats_blanks_and_nan():
    assert import_projections._id_text(float("nan")) is None
    assert import_projections._id_text("123.0") == "123"
    assert import_projections._id_text("  ") is None


def test_load_projections_requires_a_name_column(tmp_path):
    path = tmp_path / "ZIPS-2027-batting.csv"
    path.write_text("PA,AB\n1,1\n")
    with pytest.raises(ValueError, match="no Name column"):
        import_projections.load_projections(path, "batting", ({}, {}))


def test_pitching_file_imports_and_warns_about_unmatched_players(tmp_path, conn, capsys):
    make_db(conn)
    path = tmp_path / "ZIPS-2027-pitching.csv"
    path.write_text(PITCHING_CSV)

    assert import_projections.import_projections(path, conn, "s1") == ("ZIPS", 2027, "pitching", 1)

    assert "no player id for 1 players" in capsys.readouterr().err
    assert conn.execute("SELECT name, ip, era FROM pitching_projections").fetchall() == [("Ace Pitcher", 180.1, 3.1)]


def test_main_without_files_or_input_reports_errors(tmp_path, conn, capsys):
    assert import_projections.main([str(tmp_path)]) == 1
    assert "No projection files found" in capsys.readouterr().err

    make_db(conn)
    conn.commit()
    assert import_projections.main([str(tmp_path / "ZIPS-2027-batting.csv")]) == 1
    assert "Missing input file" in capsys.readouterr().err


def test_main_warns_when_scores_cannot_be_calculated(tmp_path, conn, capsys, monkeypatch):
    make_db(conn)
    conn.commit()
    (tmp_path / "ZIPS-2027-batting.csv").write_text(CSV, encoding="utf-8")

    def fail():
        raise ValueError("no stats")

    monkeypatch.setattr(import_projections.projections, "save_projection_scores", fail)

    assert import_projections.main([str(tmp_path)]) == 0
    assert "projection scores not calculated (no stats)" in capsys.readouterr().err

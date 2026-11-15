import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import import_players  # noqa: E402
import import_rankings  # noqa: E402
from player_ids import PlayerIndex  # noqa: E402


def test_parse_filename():
    assert import_rankings.parse_filename("bp-2026-dynasty.csv") == ("bp", 2026)
    with pytest.raises(ValueError):
        import_rankings.parse_filename("bp-2026.csv")


def test_load_rankings_matches_players_and_types(tmp_path):
    path = tmp_path / "bp-2026-dynasty.csv"
    path.write_text(
        '\ufeff"Rank","Change","Player","Team","Pos","July"\n'
        '"1","0","Bobby Witt Jr.","KC","SS","1"\n'
        '"2","-3","Paul Skenes","PIT","RHP","—"\n'
        '"3","5","Unknown Guy","NYM","OF","8"\n',
        encoding="utf-8",
    )
    index = PlayerIndex([("w1", "Bobby Witt Jr.", None, "KC", "SS", True),
                         ("s1", "Paul Skenes", None, "PIT", "P", True)])
    df = import_rankings.load_rankings(path, index)
    assert df["player_id"].tolist() == ["w1", "s1", None]
    assert df["player_type"].tolist() == ["Batter", "Pitcher", "Batter"]
    assert df["change"].tolist() == [0, -3, 5]
    assert df["previous_rank"].tolist() == [1, None, 8]


RANKINGS_CSV = '"Rank","Change","Player","Team","Pos"\n"1","0","Bobby Witt Jr.","KC","SS"\n"2","1","Nobody","SEA","RHP"\n'


def make_db(conn):
    conn.executescript(import_players.SCHEMA)
    conn.execute(
        "INSERT INTO players (player_id, name, cbs_name, team, pos, active) VALUES "
        "('w1', 'Bobby Witt Jr.', 'Bobby Witt Jr.', 'KC', 'SS', 1)"
    )
    conn.executescript(import_rankings.SCHEMA)


def test_find_files_keeps_explicit_paths_and_skips_other_csvs(tmp_path):
    explicit = tmp_path / "x.csv"
    folder = tmp_path / "d"
    folder.mkdir()
    (folder / "bp-2026-dynasty.csv").write_text("")
    (folder / "notes.csv").write_text("")

    files, skipped = import_rankings.find_files([explicit, folder])

    assert [f.name for f in files] == ["x.csv", "bp-2026-dynasty.csv"]
    assert [f.name for f in skipped] == ["notes.csv"]


def test_load_rankings_requires_rank_and_player_columns(tmp_path):
    path = tmp_path / "bp-2026-dynasty.csv"
    path.write_text("Team,Pos\nKC,SS\n")
    with pytest.raises(ValueError, match="missing column"):
        import_rankings.load_rankings(path, PlayerIndex([]))


def test_import_rankings_inserts_rows_and_warns_about_unmatched(tmp_path, conn, capsys):
    make_db(conn)
    path = tmp_path / "bp-2026-dynasty.csv"
    path.write_text(RANKINGS_CSV)

    assert import_rankings.import_rankings(path, conn, "s1", PlayerIndex.load(conn)) == ("bp", 2026, 2)

    assert "no player id for 1 players" in capsys.readouterr().err
    assert conn.execute("SELECT `rank`, player_id, player_type FROM rankings_dynasty ORDER BY `rank`").fetchall() == [
        (1, "w1", "Batter"), (2, None, "Pitcher"),
    ]


def test_main_imports_a_folder_and_reports_problems(tmp_path, conn, capsys):
    folder = tmp_path / "rankings"
    folder.mkdir()
    (folder / "notes.csv").write_text("")
    (folder / "bp-2026-dynasty.csv").write_text(RANKINGS_CSV)
    assert import_rankings.main([str(folder)]) == 1  # no players table yet
    assert "Error:" in capsys.readouterr().err

    make_db(conn)
    conn.commit()
    assert import_rankings.main([str(folder)]) == 0
    captured = capsys.readouterr()
    assert "Skipping notes.csv" in captured.err
    assert "bp 2026 dynasty: 2 players" in captured.out

    assert import_rankings.main([str(tmp_path / "bp-2027-dynasty.csv")]) == 1
    assert "Missing input file" in capsys.readouterr().err


def test_main_with_no_ranking_files(tmp_path, capsys):
    assert import_rankings.main([str(tmp_path)]) == 1
    assert "No ranking files found" in capsys.readouterr().err

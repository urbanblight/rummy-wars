import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import import_players  # noqa: E402
import import_prospects  # noqa: E402
from player_ids import PlayerIndex  # noqa: E402

INDEX = PlayerIndex([("m1", "Jesús Made", None, "MIL", "SS", True),
                     ("a1", "Kade Anderson", None, "SEA", "P", True)])


def test_parse_filename():
    assert import_prospects.parse_filename("fangraphs-2026-batting.csv") == ("fangraphs", 2026, "batting")
    with pytest.raises(ValueError):
        import_prospects.parse_filename("bp.csv")


def test_bp_file_ignores_age_and_previous_list(tmp_path):
    path = tmp_path / "bp-2026-midseason.csv"
    path.write_text(
        '\ufeff"Rank","Change","Player","Team","POS.","SEC. POS.","Age","ETA","Level","October"\n'
        '"1","2","Jesús Made","MIL","SS","-","19.06","2027","AA","3"\n'
        '"2","-1","Kade Anderson","SEA","LHP","-","22.1","2027","AAA","1"\n',
        encoding="utf-8",
    )
    df = import_prospects.load_prospects(path, INDEX)
    assert df["player_id"].tolist() == ["m1", "a1"]
    assert df["player_type"].tolist() == ["Batter", "Pitcher"]
    assert df["rank"].tolist() == [1, 2]
    assert df["change"].tolist() == [2, -1]
    assert df["eta"].tolist() == [2027, 2027]
    assert df["age"].isna().all() and df["sec_pos"].isna().all()


def test_fangraphs_file_keeps_only_its_player_type(tmp_path):
    path = tmp_path / "fangraphs-2026-pitching.csv"
    path.write_text(
        "Name,Pos,Org,Top 100,Org Rk,Age,TJ Date,FB Type,FB,SL,CB,CH,CMD,Sits,Tops,FV,PlayerId\n"
        'Jesús Made,SS,MIL,1,1,19.4,,,,,,,,,,65,sa1\n'
        'Kade Anderson,SP,SEA,,3,22.2,4/1/22,Rise,70 / 70,,,,30 / 45,92-95,97,60,37238\n',
        encoding="utf-8",
    )
    df = import_prospects.load_prospects(path, INDEX)
    assert df["name"].tolist() == ["Kade Anderson"]
    assert df["player_type"].tolist() == ["Pitcher"]
    assert df["rank"].isna().all()
    assert df["org_rank"].tolist() == [3]
    assert str(df["tj_date"].iloc[0]) == "2022-04-01"
    assert df["fb"].tolist() == ["70 / 70"]


def test_mlb_combined_file_imports_rank_and_skips_blank_rows(tmp_path):
    path = tmp_path / "mlb-2026-combined.csv"
    path.write_text(
        "Rk,Player,Tm,Age,L,PA,AB,R,H\n"
        "1,Jesús Made,,19,AA,558,491,87,133\n"
        ",,,,,,,,\n"
        "3,Unknown Guy,,20,ALL (2),520,449,78,145\n",
        encoding="utf-8",
    )
    df = import_prospects.load_prospects(path, INDEX)
    assert df["name"].tolist() == ["Jesús Made", "Unknown Guy"]
    assert df["rank"].tolist() == [1, 3]
    assert df["player_id"].tolist()[0] == "m1"
    assert df["player_type"].tolist() == ["Batter", "Batter"]


def make_db(conn):
    conn.executescript(import_players.SCHEMA)
    conn.execute(
        "INSERT INTO players (player_id, name, cbs_name, team, pos, active) VALUES "
        "('m1', 'Jesus Made', 'Jesus Made', 'MIL', 'SS', 1)"
    )
    conn.executescript(import_prospects.SCHEMA)


BP_CSV = (
    '"Rank","Change","Player","Team","POS.","ETA"\n'
    '"1","2","Jesus Made","MIL","SS","2027"\n'
    '"2","1","Unknown Guy","SEA","LHP","2028"\n'
)


def test_find_files_keeps_explicit_paths_and_skips_other_csvs(tmp_path):
    explicit = tmp_path / "x.csv"
    folder = tmp_path / "d"
    folder.mkdir()
    (folder / "bp-2026-midseason.csv").write_text("")
    (folder / "notes.csv").write_text("")

    files, skipped = import_prospects.find_files([explicit, folder])

    assert [f.name for f in files] == ["x.csv", "bp-2026-midseason.csv"]
    assert [f.name for f in skipped] == ["notes.csv"]


def test_load_prospects_requires_a_name_column(tmp_path):
    path = tmp_path / "bp-2026-midseason.csv"
    path.write_text("Rank,Team\n1,MIL\n")
    with pytest.raises(ValueError, match="no Player/Name column"):
        import_prospects.load_prospects(path, INDEX)


def test_import_prospects_inserts_rows_and_warns_about_unmatched(tmp_path, conn, capsys):
    make_db(conn)
    path = tmp_path / "bp-2026-midseason.csv"
    path.write_text(BP_CSV)

    result = import_prospects.import_prospects(path, conn, "s1", PlayerIndex.load(conn))

    assert result == ("bp", 2026, "midseason", 2)
    assert "no player id for 1 players" in capsys.readouterr().err
    assert conn.execute("SELECT `rank`, player_id, player_type FROM rankings_prospects ORDER BY `rank`").fetchall() == [
        (1, "m1", "Batter"), (2, None, "Pitcher"),
    ]


def test_main_imports_a_folder_and_reports_problems(tmp_path, conn, capsys):
    folder = tmp_path / "prospects"
    folder.mkdir()
    (folder / "notes.csv").write_text("")
    (folder / "bp-2026-midseason.csv").write_text(BP_CSV)

    assert import_prospects.main([str(folder)]) == 1  # no players table yet
    assert "Error:" in capsys.readouterr().err

    make_db(conn)
    conn.commit()
    assert import_prospects.main([str(folder)]) == 0
    captured = capsys.readouterr()
    assert "Skipping notes.csv" in captured.err
    assert "bp 2026 midseason: 2 players" in captured.out

    assert import_prospects.main([str(tmp_path / "bp-2026-midseason.csv")]) == 1
    assert "Missing input file" in capsys.readouterr().err


def test_main_with_no_prospect_files(tmp_path, capsys):
    assert import_prospects.main([str(tmp_path)]) == 1
    assert "No prospect files found" in capsys.readouterr().err

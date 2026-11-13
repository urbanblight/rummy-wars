import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

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

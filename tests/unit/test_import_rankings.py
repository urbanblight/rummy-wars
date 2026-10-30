import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

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

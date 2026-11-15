import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import import_players  # noqa: E402
from player_ids import PlayerIndex  # noqa: E402

HEADER = "IDPLAYER,PLAYERNAME,TEAM,POS,IDFANGRAPHS,MLBID,CBSID,CBSNAME,BPID,ACTIVE\n"


def write_map(path, *rows):
    path.write_text(HEADER + "\n".join(rows) + "\n")
    return path


def test_import_players_upserts_and_is_idempotent(tmp_path, conn):
    source = write_map(
        tmp_path / "map.csv",
        "hicksli01,Liam Hicks,TB,C,25001,680000,123,Liam Hicks,99,Y",
        "oldtimer01,Old Timer,N/A,P,sa1,,,,,N",
    )
    assert import_players.import_players(source) == 2
    assert import_players.import_players(source) == 2

    write_map(source, "hicksli01,Liam Hicks,BOS,C,25001,680000,123,Liam Hicks,99,Y")
    import_players.import_players(source)
    conn.commit()
    assert [tuple(row) for row in conn.execute(
        "SELECT player_id, team, fangraphs_id, mlb_id, active FROM players ORDER BY 1"
    ).fetchall()] == [
        ("hicksli01", "BOS", "25001", "680000", 1),
        ("oldtimer01", None, "sa1", None, 0),  # kept even though it left the map
    ]


def test_load_players_requires_the_id_columns(tmp_path):
    source = tmp_path / "map.csv"
    source.write_text("NAME\nx\n")
    with pytest.raises(ValueError, match="IDPLAYER"):
        import_players.load_players(source)


def test_main_reports_a_missing_file(tmp_path, capsys):
    assert import_players.main(["--source", str(tmp_path / "nope.csv")]) == 1
    assert "Missing input file" in capsys.readouterr().err


def make_index(*rows):
    return PlayerIndex([(r[0], r[1], r[2], r[3], r[4], 1 if len(r) < 6 else r[5]) for r in rows])


def test_resolve_matches_cbs_name_and_drops_suffixes():
    index = make_index(("tatisfe02", "Fernando Tatis Jr.", "Fernando Tatis", "SD", "OF"))
    assert index.resolve("Fernando Tatis Jr.", "SD") == "tatisfe02"
    assert index.resolve("Unknown Guy", "SD") is None


def test_resolve_disambiguates_by_team_type_and_active_flag():
    index = make_index(
        ("smithwi01", "Will Smith", "Will Smith", "LAD", "C"),
        ("smithwi02", "Will Smith", "Will Smith", "KC", "P"),
        ("smithwi03", "Will Smith", "Will Smith", "KC", "P", 0),
    )
    assert index.resolve("Will Smith", "LAD", "Batter") == "smithwi01"
    assert index.resolve("Will Smith", "KC", "Pitcher") == "smithwi02"
    assert index.resolve("Will Smith", "SD", "Pitcher") == "smithwi02"
    assert make_index(
        ("a1", "A Guy", "A Guy", "SD", "P"), ("a2", "A Guy", "A Guy", "SD", "P")
    ).resolve("A Guy", "SD", "Pitcher") is None


def test_resolve_two_way_players_and_team_aliases_and_nicknames():
    index = make_index(
        ("ohtansh01", "Shohei Ohtani", "Shohei Ohtani", "LAD", "DH"),
        ("ohtansh01p", "Shohei Ohtani", "Shohei Ohtani", "LAD", "DH"),
        ("varlalo01", "Louis Varland", "Louis Varland", "TOR", "P"),
        ("rutleje01", "Jean Rutledge", "Jean Rutledge", "CHW", "P"),
    )
    assert index.resolve("Shohei Ohtani", "LAD", "Batter") == "ohtansh01"
    assert index.resolve("Shohei Ohtani", "LAD", "Pitcher") == "ohtansh01p"
    assert index.resolve("Louie Varland", "TOR", "Pitcher") == "varlalo01"
    assert index.resolve("Louie Varland", "BOS", "Pitcher") is None
    assert make_index(
        ("x1", "Same Name", "Same Name", "WSN", "P"), ("x2", "Same Name", "Same Name", "SD", "P")
    ).resolve("Same Name", "WAS") == "x1"


def test_load_players_downloads_urls_and_fills_missing_columns(monkeypatch):
    calls = []

    class Response:
        text = "IDPLAYER,PLAYERNAME,FANGRAPHSID\nabc01,A Player,77\n,Nameless,1\n"

        def raise_for_status(self):
            calls.append("checked")

    def fake_get(url, headers, timeout):
        calls.append(url)
        return Response()

    monkeypatch.setattr(import_players.requests, "get", fake_get)

    df = import_players.load_players("https://example.test/map.csv")

    assert calls == ["https://example.test/map.csv", "checked"]
    assert df["player_id"].tolist() == ["abc01"]
    assert df["fangraphs_id"].tolist() == ["77"]
    assert df["cbs_name"].tolist() == [None]  # absent from the export
    assert df["active"].tolist() == [0]


def test_main_imports_and_reports_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(import_players, "import_players", lambda source: 3)
    assert import_players.main(["--source", "x.csv"]) == 0
    assert "Imported 3 players" in capsys.readouterr().out

    def boom(source):
        raise OSError("disk on fire")

    monkeypatch.setattr(import_players, "import_players", boom)
    assert import_players.main([]) == 1
    assert "Error: disk on fire" in capsys.readouterr().err

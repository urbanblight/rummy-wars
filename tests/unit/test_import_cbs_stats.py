import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import import_cbs_stats  # noqa: E402
import import_players  # noqa: E402
import import_teams  # noqa: E402
from player_ids import PlayerIndex  # noqa: E402

# (player_id, name, cbs_name, team, pos, active)
PLAYERS = [
    ("judgeaa01", "Aaron Judge", "Aaron Judge", "NYY", "OF", 1),
    ("hicksli01", "Liam Hicks", "Liam Hicks", "TB", "C", 1),
    ("colege01", "Gerrit Cole", "Gerrit Cole", "NYY", "P", 1),
]


def write_batting(path):
    path.write_text(
        "Batters\n"
        "Rank,Player,Avail,HR,Unnamed: 4\n"
        '2,"Aaron Judge RF | NYY",The Outfield,40,\n'
        + '1,"Aaron Judge RF | NYY",The Outfield,41,\n'  # duplicate, better rank wins
        + '3,"Liam Hicks C,1B | TB",FA,5,\n'
        + '4,"Mystery Man 2B | BOS",Ghost Team,1,\n'
        + '5,Bare Name,FA,0,\n'
        + ",,Report Updated,,\n"
    )


def write_pitching(path):
    path.write_text(
        "Pitchers\n"
        "Rank,Player,Avail,W,INNs\n"
        '1,"Gerrit Cole SP | NYY",The Outfield,10,174.2\n'
    )


def test_innings_to_float():
    assert import_cbs_stats.innings_to_float(174.2) == pytest.approx(174 + 2 / 3)
    assert import_cbs_stats.innings_to_float(5.0) == 5


def test_load_team_ids_requires_teams_table(database):
    with pytest.raises(RuntimeError, match="import_teams"):
        import_cbs_stats.load_team_ids()


def test_load_team_ids_and_player_index(conn):
    conn.executescript(import_teams.SCHEMA)
    conn.executescript(import_players.SCHEMA)
    conn.execute("INSERT INTO managers (name, email) VALUES ('M', 'm@x')")
    conn.execute("INSERT INTO teams VALUES (1, 'The Outfield', 'OF', 1)")
    conn.execute(
        "INSERT INTO players (player_id, name, cbs_name, team, pos, active) "
        "VALUES ('judgeaa01', 'Aaron Judge', 'Aaron Judge', 'NYY', 'OF', 1)"
    )
    conn.commit()

    assert import_cbs_stats.load_team_ids() == {"the outfield": 1}
    assert import_cbs_stats.load_player_index().resolve("Aaron Judge") == "judgeaa01"


def test_load_csv_parses_players_owners_and_duplicates(tmp_path, capsys):
    path = tmp_path / "stats-batting.csv"
    write_batting(path)

    df = import_cbs_stats.load_csv(path, {"the outfield": 1}, PlayerIndex(PLAYERS), "Batter")

    assert df["rank"].tolist() == [1, 3, 4, 5]
    judge = df.iloc[0]
    assert (judge["name"], judge["positions"], judge["team"], judge["player_id"]) == ("Aaron Judge", "RF", "NYY", "judgeaa01")
    assert judge["HR"] == 41
    assert df["team_id"].tolist()[0] == 1
    assert df["team_id"].isna().tolist() == [False, True, True, True]
    assert df.iloc[3]["positions"] == "" and df.iloc[3]["team"] == "UNKNOWN"
    assert "Unnamed: 4" not in df.columns
    err = capsys.readouterr().err
    assert "no player id for 2 batters" in err
    assert "no matching team for ['Ghost Team']" in err


def test_load_csv_quiet_when_everything_matches(tmp_path, capsys):
    path = tmp_path / "stats-pitching.csv"
    write_pitching(path)

    df = import_cbs_stats.load_csv(path, {"the outfield": 1}, PlayerIndex(PLAYERS), "Pitcher")

    assert df["player_id"].tolist() == ["colege01"]
    assert capsys.readouterr().err == ""


def stub_pipeline(monkeypatch, sgp_error=None):
    calls = []

    def save_factors():
        if sgp_error:
            raise sgp_error
        calls.append("sgp")

    monkeypatch.setattr(import_cbs_stats.calculate_sgp, "save_factors", save_factors)
    monkeypatch.setattr(import_cbs_stats.zscores, "save_z_scores", lambda: calls.append("z"))
    monkeypatch.setattr(import_cbs_stats.projections, "save_projection_scores", lambda: calls.append("proj"))
    return calls


def seed_reference_tables(conn):
    conn.executescript(import_teams.SCHEMA)
    conn.executescript(import_players.SCHEMA)
    conn.execute("INSERT INTO managers (name, email) VALUES ('M', 'm@x')")
    conn.execute("INSERT INTO teams VALUES (1, 'The Outfield', 'OF', 1)")
    conn.execute(
        "INSERT INTO players (player_id, name, cbs_name, team, pos, active) "
        "VALUES ('judgeaa01', 'Aaron Judge', 'Aaron Judge', 'NYY', 'OF', 1)"
    )
    conn.commit()


@pytest.mark.parametrize("sgp_error", [None, ValueError("no standings")])
def test_import_stats_writes_tables_and_runs_pipeline(tmp_path, conn, monkeypatch, capsys, sgp_error):
    seed_reference_tables(conn)
    folder = tmp_path / "2026"
    folder.mkdir()
    write_batting(folder / "stats-batting.csv")
    write_pitching(folder / "stats-pitching.csv")
    calls = stub_pipeline(monkeypatch, sgp_error)

    assert import_cbs_stats.import_stats("2026", tmp_path) == {"batting": 4, "pitching": 1}

    assert calls == (["z", "proj"] if sgp_error else ["sgp", "z", "proj"])
    assert ("SGP factors not updated" in capsys.readouterr().err) == bool(sgp_error)
    conn.commit()
    assert conn.execute("SELECT INNs FROM pitching").fetchone()[0] == pytest.approx(174 + 2 / 3)
    assert conn.execute("SELECT COUNT(*) FROM batting WHERE team_id = 1").fetchone()[0] == 1


def test_main_reports_success_and_failures(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(import_cbs_stats, "import_stats", lambda year, data_dir: {"batting": 2, "pitching": 3})
    assert import_cbs_stats.main(["--year", "2025", "--data-dir", str(tmp_path)]) == 0
    assert "Imported 2 batters and 3 pitchers" in capsys.readouterr().out

    def missing(year, data_dir):
        raise FileNotFoundError(2, "nope", "stats-batting.csv")

    monkeypatch.setattr(import_cbs_stats, "import_stats", missing)
    assert import_cbs_stats.main([]) == 1
    assert "Missing input file: stats-batting.csv" in capsys.readouterr().err

    def broken(year, data_dir):
        raise RuntimeError("No teams table")

    monkeypatch.setattr(import_cbs_stats, "import_stats", broken)
    assert import_cbs_stats.main([]) == 1
    assert "No teams table" in capsys.readouterr().err

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_teams  # noqa: E402


def test_team_ids_follow_cbs_and_skip_three():
    assert [import_teams.cbs_team_id(n) for n in range(1, 8)] == [1, 2, 4, 5, 6, 7, 8]


def test_load_teams_assigns_cbs_ids_in_file_order(tmp_path):
    csv = tmp_path / "teams.csv"
    rows = "\n".join(f",Team {n},T{n},Manager {n},m{n}@x.com,," for n in range(1, 5))
    csv.write_text("Table 1\n,,,,,,\n,TEAM NAME,TEAM ABBREV.,MANAGER,MANAGER EMAIL,,\n" + rows + "\n")

    teams = import_teams.load_teams(csv)

    assert teams["id"].tolist() == [1, 2, 4, 5]
    assert teams["name"].tolist() == ["Team 1", "Team 2", "Team 3", "Team 4"]


def write_teams(path, rows):
    body = "\n".join(f",{n},{a},{m},{e},," for n, a, m, e in rows)
    path.write_text("Table 1\n,,,,,,\n,TEAM NAME,TEAM ABBREV.,MANAGER,MANAGER EMAIL,,\n" + body + "\n")


def test_normalize_team_name():
    assert import_teams.normalize_team_name(" Bob\\'s Team ") == "bob's team"


def test_import_teams_upserts_and_prunes(tmp_path, conn):
    csv = tmp_path / "teams.csv"
    write_teams(csv, [("One", "O", "Ann", "a@x"), ("Two", "T", "Bob", "b@x"), ("Three", "H", "Cy", "c@x")])
    assert import_teams.import_teams(csv) == 3

    write_teams(csv, [("One Renamed", "O", "ann", "new@x"), ("Two", "T", "Bob", "b@x")])
    assert import_teams.import_teams(csv) == 2

    conn.commit()
    assert [tuple(r) for r in conn.execute("SELECT id, name FROM teams ORDER BY id").fetchall()] == [(1, "One Renamed"), (2, "Two")]
    assert conn.execute("SELECT email FROM managers WHERE name = 'Ann'").fetchone()[0] == "new@x"


def test_main_success_and_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(import_teams, "import_teams", lambda path: 4)
    assert import_teams.main(["--csv", str(tmp_path / "t.csv")]) == 0
    assert "Imported 4 teams" in capsys.readouterr().out

    def missing(path):
        raise FileNotFoundError(2, "nope", "t.csv")

    monkeypatch.setattr(import_teams, "import_teams", missing)
    assert import_teams.main([]) == 1
    assert "Missing input file: t.csv" in capsys.readouterr().err

    def broken(path):
        raise import_teams.db.NotConfigured("no db")

    monkeypatch.setattr(import_teams, "import_teams", broken)
    assert import_teams.main([]) == 1
    assert "Error: no db" in capsys.readouterr().err

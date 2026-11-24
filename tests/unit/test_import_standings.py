import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import import_standings  # noqa: E402
import import_teams  # noqa: E402

CSV = "\n".join([
    "Overall Rank,Team,Batting,Pitching,Total,Change,Behind",
    '1,The Outfield,10,9,19,1.5,0',
    '2,"Bob\\\'s Team",8,7,15,-1,4',
    "",
    "Batting Breakdown",
    "Team,HR,Pts,Chg",
    "The Outfield,200,2,0.5",
    "Bob's Team,150,1,-0.5",
    "",
    "Pitching Breakdown",
    "Team,ERA,Pts,Chg",
    "The Outfield,3.5,2,0",
    "Bob's Team,4.5,1,0",
    "",
])

TSV = (
    "Overall Rank\tTeam\tBatting\tPitching\tTotal\tChange\tBehind\n"
    "1\tThe Outfield\t10\t9\t19\t1\t0\n"
    "2\tBob\\'s Team\t8\t7\t15\t-1\t4Batting Breakdown\n"
    "Team\tHR\tPts\tChg\n"
    "The Outfield\t200\t2\t0\n"
    "Bob's Team\t150\t1\t0\n"
)


def test_normalize_team_name():
    assert import_standings.normalize_team_name("The Bob\\'s  Team!") == "bobsteam"
    assert import_standings.normalize_team_name("The") == "the"


def test_split_line_handles_tabs_and_csv():
    assert import_standings.split_line("a\\b\t c \t\t") == ["ab", "c"]
    assert import_standings.split_line('1,"Smith, Jr.",2,,') == ["1", "Smith, Jr.", "2"]
    assert import_standings.split_line("") == []


def test_number():
    assert import_standings.number("1.5") == 1.5


@pytest.mark.parametrize("text", [CSV, TSV])
def test_parse_standings_both_formats(text):
    overall, categories = import_standings.parse_standings(text)

    assert [r["rank"] for r in overall] == [1, 2]
    assert overall[0]["total"] == 19
    assert overall[1]["behind"] == 4
    assert {c["team"] for c in categories} == {"The Outfield", "Bob's Team"}
    assert categories[0]["type"] == "batting" and categories[0]["category"] == "HR"


def seed_teams(conn, year=2025):
    conn.executescript(import_teams.SCHEMA)
    conn.execute("INSERT INTO managers (name, email) VALUES ('M', 'm@x')")
    conn.executemany("INSERT INTO teams VALUES (%s, %s, 'T', 1)", [(1, "The Outfield"), (2, "Bobs Team")])
    conn.executemany(
        "INSERT INTO teams_history VALUES (%s, %s, %s, 1)",
        [(1, year, "The Outfield"), (2, year, "Bob's Team")],
    )
    conn.commit()


def test_load_team_ids_requires_history(conn):
    conn.executescript(import_teams.SCHEMA)
    conn.commit()
    with pytest.raises(RuntimeError, match="2024"):
        import_standings.load_team_ids(conn, 2024)


def test_import_standings_replaces_snapshot(tmp_path, conn):
    seed_teams(conn)
    path = tmp_path / "2025.csv"
    path.write_text(CSV)

    assert import_standings.import_standings(path, 2025, "2025-12-31") == (2, 4)
    assert import_standings.import_standings(path, 2025, "2025-12-31") == (2, 4)

    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM standings").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM standings_categories").fetchone()[0] == 4


def test_import_standings_rejects_empty_and_unknown_teams(tmp_path, conn):
    seed_teams(conn)
    empty = tmp_path / "empty.csv"
    empty.write_text("nothing here\n")
    with pytest.raises(ValueError, match="No overall standings"):
        import_standings.import_standings(empty, 2025, "2025-12-31")

    other = tmp_path / "other.csv"
    other.write_text(CSV.replace("The Outfield", "Stranger"))
    with pytest.raises(ValueError, match="Stranger"):
        import_standings.import_standings(other, 2025, "2025-12-31")


def test_main_success_infers_year_and_default_date(monkeypatch, capsys, tmp_path):
    seen = []
    monkeypatch.setattr(import_standings, "import_standings", lambda p, y, a: seen.append((y, a)) or (2, 4))

    assert import_standings.main([str(tmp_path / "standings-2024.csv")]) == 0
    assert import_standings.main([str(tmp_path / "x.csv"), "--year", "2023", "--as-of", "2023-06-01"]) == 0

    assert seen == [(2024, "2024-12-31"), (2023, "2023-06-01")]
    assert "Imported 2024 standings as of 2024-12-31: 2 teams, 4 category rows" in capsys.readouterr().out


def test_main_argument_errors(tmp_path):
    with pytest.raises(SystemExit):
        import_standings.main([str(tmp_path / "nodigits.csv")])
    with pytest.raises(SystemExit):
        import_standings.main([str(tmp_path / "2024.csv"), "--as-of", "yesterday"])


def test_main_reports_import_errors(monkeypatch, capsys, tmp_path):
    def fail(path, year, as_of):
        raise ValueError("bad file")

    monkeypatch.setattr(import_standings, "import_standings", fail)
    assert import_standings.main([str(tmp_path / "2024.csv")]) == 1
    assert "Error: bad file" in capsys.readouterr().err

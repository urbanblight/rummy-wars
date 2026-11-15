import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import calculate_sgp  # noqa: E402
import refresh  # noqa: E402


def test_standings_files_lists_only_year_named_csvs_oldest_first(tmp_path):
    folder = tmp_path / "standings"
    folder.mkdir()
    for name in ("2025.csv", "2023.csv", "notes.csv", "2024-partial.csv", "2024.txt"):
        (folder / name).write_text("")

    assert [year for year, _ in refresh.standings_files(tmp_path)] == [2023, 2025]


def test_save_factors_replaces_stored_factors(conn):
    conn.execute(
        "CREATE TABLE standings_categories (season_year INT, as_of_date VARCHAR(10), team_id INT,"
        " category VARCHAR(64), category_type VARCHAR(16), value DOUBLE, points DOUBLE, points_change DOUBLE)"
    )
    conn.executemany(
        "INSERT INTO standings_categories VALUES (2026, '2026-12-31', %s, 'HR', 'batting', %s, %s, 0)",
        [(1, 10, 1), (2, 20, 2), (3, 30, 3)],
    )
    conn.commit()

    assert calculate_sgp.save_factors() == 1
    assert calculate_sgp.save_factors() == 1
    assert [tuple(row) for row in conn.execute("SELECT category, factor FROM sgp_factors").fetchall()] == [("HR", pytest.approx(10.0))]


def test_import_lineup_files_imports_the_folder_and_handles_none(tmp_path, conn):
    import import_lineups
    import import_players

    conn.execute("CREATE TABLE teams (id INT PRIMARY KEY, name VARCHAR(255))")
    conn.execute("INSERT INTO teams VALUES (7, 'The Outfield')")
    conn.executescript(import_players.SCHEMA)
    conn.commit()

    assert refresh.import_lineup_files(tmp_path) == 0  # no lineups folder

    folder = tmp_path / "lineups"
    folder.mkdir()
    (folder / "roster-overview-7-20260928.csv").write_text(
        'Batters\n,Pos,Players\n,C,"Liam Hicks C,1B | TB ",x,452,102,94%,77%\n'
    )
    (folder / "notes.csv").write_text("")

    assert refresh.import_lineup_files(tmp_path) == 1
    assert [tuple(row) for row in conn.execute("SELECT name, slot FROM lineups").fetchall()] == [("Liam Hicks", "C")]


def test_import_lineup_files_skips_when_no_roster_files(tmp_path, capsys):
    (tmp_path / "lineups").mkdir()
    (tmp_path / "lineups" / "notes.csv").write_text("")

    assert refresh.import_lineup_files(tmp_path) == 0
    assert "Skipping notes.csv" in capsys.readouterr().err


def test_refresh_runs_every_step_in_order(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(refresh.import_teams, "import_teams", lambda p: calls.append("teams") or 8)
    monkeypatch.setattr(refresh.import_players, "import_players", lambda s: calls.append(("players", s)) or 100)
    monkeypatch.setattr(
        refresh.import_standings, "import_standings",
        lambda path, season, as_of: calls.append(("standings", season, as_of)) or (8, 80),
    )
    monkeypatch.setattr(refresh, "import_lineup_files", lambda d: calls.append("lineups") or 0)
    monkeypatch.setattr(
        refresh.import_cbs_stats, "import_stats",
        lambda year, d: calls.append(("stats", year)) or {"batting": 5, "pitching": 4},
    )
    (tmp_path / "standings").mkdir()
    (tmp_path / "standings" / "2025.csv").write_text("")

    refresh.refresh("2026", tmp_path)  # no player-id-map.csv: falls back to the download URL
    out = capsys.readouterr().out
    assert calls == [
        "teams", ("players", refresh.import_players.DEFAULT_SOURCE),
        ("standings", 2025, "2025-12-31"), "lineups", ("stats", "2026"),
    ]
    assert "Lineups: none found" in out and "Stats 2026: 5 batters, 4 pitchers" in out

    (tmp_path / "player-id-map.csv").write_text("")
    monkeypatch.setattr(refresh, "import_lineup_files", lambda d: 2)
    calls.clear()
    refresh.refresh("2026", tmp_path)
    assert calls[1] == ("players", tmp_path / "player-id-map.csv")
    assert "Lineups: imported 2 lineup files" in capsys.readouterr().out


def test_main_reports_success_and_failures(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(refresh, "refresh", lambda year, data_dir: None)
    assert refresh.main(["--year", "2025", "--data-dir", str(tmp_path)]) == 0
    assert "Refreshed the stats database" in capsys.readouterr().out

    for error, expected in [
        (FileNotFoundError(2, "nope", "teams.csv"), "Missing input file: teams.csv"),
        (ValueError("bad standings"), "Error: bad standings"),
    ]:
        def fail(year, data_dir, error=error):
            raise error

        monkeypatch.setattr(refresh, "refresh", fail)
        assert refresh.main([]) == 1
        assert expected in capsys.readouterr().err

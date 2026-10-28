import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import calculate_sgp  # noqa: E402
import refresh  # noqa: E402


def test_standings_files_lists_only_year_named_csvs_oldest_first(tmp_path):
    folder = tmp_path / "standings"
    folder.mkdir()
    for name in ("2025.csv", "2023.csv", "notes.csv", "2024-partial.csv", "2024.txt"):
        (folder / name).write_text("")

    assert [year for year, _ in refresh.standings_files(tmp_path)] == [2023, 2025]


def test_save_factors_replaces_stored_factors(tmp_path):
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE standings_categories (season_year, as_of_date, team_id, category,"
            " category_type, value, points, points_change)"
        )
        conn.executemany(
            "INSERT INTO standings_categories VALUES (2026, '2026-12-31', ?, 'HR', 'batting', ?, ?, 0)",
            [(1, 10, 1), (2, 20, 2), (3, 30, 3)],
        )

    assert calculate_sgp.save_factors(db) == 1
    assert calculate_sgp.save_factors(db) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT category, factor FROM sgp_factors").fetchall() == [("HR", 10.0)]

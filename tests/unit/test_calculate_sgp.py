import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import calculate_sgp  # noqa: E402


def test_ewma_weights_give_latest_season_its_weight_and_sum_to_one():
    weights = calculate_sgp.ewma_weights(4, 0.5)
    assert weights == [0.125, 0.125, 0.25, 0.5]
    assert sum(weights) == pytest.approx(1)


def test_ewma_weights_reject_invalid_latest_weight():
    with pytest.raises(ValueError):
        calculate_sgp.ewma_weights(3, 0)


def test_season_slope_is_positive_for_lower_is_better_categories():
    assert calculate_sgp.season_slope([3.0, 4.0, 5.0], [3, 2, 1]) == pytest.approx(1.0)
    assert calculate_sgp.season_slope([30, 20, 10], [3, 2, 1]) == pytest.approx(10.0)


def test_calculate_uses_only_full_year_snapshots(conn):
    conn.execute(
        "CREATE TABLE standings_categories (season_year INT, as_of_date VARCHAR(10), team_id INT,"
        " category VARCHAR(64), category_type VARCHAR(16), value DOUBLE, points DOUBLE, points_change DOUBLE)"
    )
    rows = [
        (2025, "2025-12-31", t, "HR", "batting", 10 * t, t, 0) for t in (1, 2, 3)
    ] + [
        (2026, "2026-12-31", t, "HR", "batting", 20 * t, t, 0) for t in (1, 2, 3)
    ] + [
        (2026, "2026-06-01", t, "HR", "batting", 99 * t, t, 0) for t in (1, 2, 3)
    ]
    conn.executemany("INSERT INTO standings_categories VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", rows)

    (category, kind, factor, first, last, *_), = calculate_sgp.calculate(conn, 0.5)

    assert (category, kind, first, last) == ("HR", "batting", 2025, 2026)
    assert factor == pytest.approx(0.5 * 20 + 0.5 * 10)


def test_calculate_requires_full_year_standings(conn):
    conn.execute(
        "CREATE TABLE standings_categories (season_year INT, as_of_date VARCHAR(10), team_id INT,"
        " category VARCHAR(64), category_type VARCHAR(16), value DOUBLE, points DOUBLE, points_change DOUBLE)"
    )
    with pytest.raises(ValueError, match="import_standings"):
        calculate_sgp.calculate(conn, 0.5)


def test_main_reports_success_and_failure(monkeypatch, capsys):
    monkeypatch.setattr(calculate_sgp, "save_factors", lambda weight: 3)
    assert calculate_sgp.main(["--latest-weight", "0.7"]) == 0
    assert "Saved SGP factors for 3 categories" in capsys.readouterr().out

    def fail(weight):
        raise ValueError("no standings")

    monkeypatch.setattr(calculate_sgp, "save_factors", fail)
    assert calculate_sgp.main([]) == 1
    assert "Error: no standings" in capsys.readouterr().err

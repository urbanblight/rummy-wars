import sqlite3
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


def test_calculate_uses_only_full_year_snapshots():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE standings_categories (season_year, as_of_date, team_id, category,"
        " category_type, value, points, points_change)"
    )
    rows = [
        (2025, "2025-12-31", t, "HR", "batting", 10 * t, t, 0) for t in (1, 2, 3)
    ] + [
        (2026, "2026-12-31", t, "HR", "batting", 20 * t, t, 0) for t in (1, 2, 3)
    ] + [
        (2026, "2026-06-01", t, "HR", "batting", 99 * t, t, 0) for t in (1, 2, 3)
    ]
    conn.executemany("INSERT INTO standings_categories VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    (category, kind, factor, first, last, *_), = calculate_sgp.calculate(conn, 0.5)

    assert (category, kind, first, last) == ("HR", "batting", 2025, 2026)
    assert factor == pytest.approx(0.5 * 20 + 0.5 * 10)

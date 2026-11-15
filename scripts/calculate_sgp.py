"""Calculate standings-gain-points (SGP) factors from imported standings.

Usage:
    python scripts/calculate_sgp.py [--latest-weight 0.5]

Run ``import_standings.py`` first. For every scoring category the script takes
each season's full-year snapshot (``as_of_date`` = December 31), fits a line of
team stat against roto points, and keeps the absolute slope: the amount of the
stat that is worth one standings point. The yearly slopes are then combined
with an exponentially weighted average: the latest season gets
``--latest-weight`` and each earlier season half the weight of the one after
it, with the leftover weight going to the oldest season so the weights sum to 1.

The result replaces the contents of the ``sgp_factors`` table.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import db  # noqa: E402

# DDL for the factors table. ``calculated_at`` is an ISO-8601 UTC string so the
# web app can show how fresh the factors are without a driver-specific type.
SCHEMA = """
CREATE TABLE IF NOT EXISTS sgp_factors (
    category VARCHAR(64) PRIMARY KEY,
    category_type VARCHAR(16) NOT NULL CHECK (category_type IN ('batting', 'pitching')),
    factor DOUBLE NOT NULL,
    first_season INT NOT NULL,
    last_season INT NOT NULL,
    latest_weight DOUBLE NOT NULL,
    calculated_at VARCHAR(32) NOT NULL
);
"""


def season_slope(values: list[float], points: list[float]) -> float:
    """Fit stat against roto points and return the stat gained per standings point.

    The sign is dropped because lower-is-better categories (ERA, WHIP) slope
    downwards while the factor is always used as a positive divisor.

    Args:
        values: Each team's stat total for one category and season.
        points: The roto points those teams earned for the stat, same order.

    Returns:
        The absolute slope of the least-squares line of ``values`` on ``points``.
    """
    return abs(float(np.polyfit(points, values, 1)[0]))


def ewma_weights(count: int, latest_weight: float) -> list[float]:
    """Exponentially decaying season weights, ordered oldest to newest.

    Each season counts ``1 - latest_weight`` times the one after it. Because a
    truncated geometric series sums to less than 1, the shortfall is added to
    the oldest season so the weights always sum to exactly 1.

    Args:
        count: Number of seasons to weight (at least 1).
        latest_weight: Weight of the newest season, in ``(0, 1]``.

    Returns:
        ``count`` weights, oldest first, summing to 1.

    Raises:
        ValueError: If ``latest_weight`` is outside ``(0, 1]``.
    """
    if not 0 < latest_weight <= 1:
        raise ValueError("latest weight must be in (0, 1]")
    weights = [latest_weight * (1 - latest_weight) ** age for age in range(count)]
    weights[-1] += 1 - sum(weights)  # oldest season takes the remainder
    return weights[::-1]


def calculate(conn: db.Connection, latest_weight: float) -> list[tuple]:
    """Compute the SGP factor of every category from full-year standings.

    Only snapshots whose ``as_of_date`` is December 31 are used, so partial
    in-season snapshots do not distort the slopes.

    Args:
        conn: Open database connection holding ``standings_categories``.
        latest_weight: Weight of the newest season (see ``ewma_weights``).

    Returns:
        One ``(category, category_type, factor, first_season, last_season,
        latest_weight, calculated_at)`` tuple per category, ready to insert
        into ``sgp_factors``.

    Raises:
        ValueError: If there are no full-year standings, or ``latest_weight``
            is invalid.
    """
    rows = conn.execute(
        """
        SELECT category, category_type, season_year, value, points
        FROM standings_categories
        WHERE as_of_date = CONCAT(season_year, '-12-31')
        ORDER BY category, season_year
        """
    ).fetchall()
    if not rows:
        raise ValueError("No full-year standings found; run scripts/import_standings.py first")

    by_category: dict[str, dict] = {}
    for category, category_type, season, value, points in rows:
        entry = by_category.setdefault(category, {"type": category_type, "seasons": {}})
        entry["seasons"].setdefault(season, ([], []))
        entry["seasons"][season][0].append(value)
        entry["seasons"][season][1].append(points)

    calculated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    for category, entry in by_category.items():
        seasons = sorted(entry["seasons"])
        slopes = [season_slope(*entry["seasons"][season]) for season in seasons]
        factor = float(np.dot(ewma_weights(len(seasons), latest_weight), slopes))
        results.append((category, entry["type"], factor, seasons[0], seasons[-1],
                        latest_weight, calculated_at))
    return results


def save_factors(latest_weight: float = 0.5) -> int:
    """Recalculate the factors and replace the contents of ``sgp_factors``.

    The factors are calculated before the table is touched so a failure leaves
    the previous factors in place.

    Args:
        latest_weight: Weight of the newest season (see ``ewma_weights``).

    Returns:
        The number of categories saved.

    Raises:
        ValueError: If there are no full-year standings or the weight is invalid.
    """
    with db.connect() as conn:
        results = calculate(conn, latest_weight)
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM sgp_factors")
        conn.executemany("INSERT INTO sgp_factors VALUES (%s, %s, %s, %s, %s, %s, %s)", results)
    return len(results)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments to parse instead of ``sys.argv[1:]`` (for tests).

    Returns:
        Process exit status: 0 on success, 1 if the factors could not be saved.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--latest-weight", type=float, default=0.5,
                        help="weight of the most recent season (default: 0.5)")
    args = parser.parse_args(argv)
    try:
        count = save_factors(args.latest_weight)
    except (ValueError, db.Error, db.NotConfigured) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Saved SGP factors for {count} categories")
    return 0


if __name__ == "__main__":
    sys.exit(main())

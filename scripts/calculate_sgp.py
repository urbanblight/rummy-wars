"""Calculate standings-gain-points (SGP) factors from imported standings.

Usage:
    python scripts/calculate_sgp.py [--latest-weight 0.5] [--db data/stats.db]

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
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS sgp_factors (
    category TEXT PRIMARY KEY,
    category_type TEXT NOT NULL CHECK (category_type IN ('batting', 'pitching')),
    factor REAL NOT NULL,
    first_season INTEGER NOT NULL,
    last_season INTEGER NOT NULL,
    latest_weight REAL NOT NULL,
    calculated_at TEXT NOT NULL
);
"""


def season_slope(values: list[float], points: list[float]) -> float:
    """Stat gained per standings point (always positive, even for ERA/WHIP)."""
    return abs(float(np.polyfit(points, values, 1)[0]))


def ewma_weights(count: int, latest_weight: float) -> list[float]:
    """Weights oldest -> newest: each season is ``1 - latest_weight`` of the next; sums to 1."""
    if not 0 < latest_weight <= 1:
        raise ValueError("latest weight must be in (0, 1]")
    weights = [latest_weight * (1 - latest_weight) ** age for age in range(count)]
    weights[-1] += 1 - sum(weights)  # oldest season takes the remainder
    return weights[::-1]


def calculate(conn: sqlite3.Connection, latest_weight: float) -> list[tuple]:
    rows = conn.execute(
        """
        SELECT category, category_type, season_year, value, points
        FROM standings_categories
        WHERE as_of_date = season_year || '-12-31'
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


def save_factors(db_path: Path, latest_weight: float = 0.5) -> int:
    """Recalculate and store the factors; returns the number of categories saved."""
    with sqlite3.connect(db_path) as conn:
        results = calculate(conn, latest_weight)
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM sgp_factors")
        conn.executemany("INSERT INTO sgp_factors VALUES (?, ?, ?, ?, ?, ?, ?)", results)
    return len(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--latest-weight", type=float, default=0.5,
                        help="weight of the most recent season (default: 0.5)")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "stats.db")
    args = parser.parse_args(argv)
    try:
        count = save_factors(args.db, args.latest_weight)
    except (ValueError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Saved SGP factors for {count} categories into {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

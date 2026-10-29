"""Projected scoring stats from the batting and pitching projection tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

try:
    from . import zscores
except ImportError:  # pragma: no cover - supports direct module imports
    import zscores

TABLES = {"Batter": "batting_projections", "Pitcher": "pitching_projections"}
# Scoring category -> projection table column, where it is not just the lowercase category.
COLUMNS = {
    "Batter": {"BA": "avg"},
    "Pitcher": {"INN": "ip", "HD": "hld", "S": "sv", "Kd9": "k9"},
}


def systems(db_path: str | Path | None = None) -> list[dict]:
    """Projection systems with imported data, as ``{"key", "label", "system", "season"}``.

    ``key`` (``ZIPS-2027``) is what the stats source selector submits. Newest season first.
    """
    path = zscores.stats_db_path(db_path)
    if not path.exists():
        return []
    found: set[tuple[str, int]] = set()
    with sqlite3.connect(path) as conn:
        for table in TABLES.values():
            try:
                found.update(conn.execute(f"SELECT DISTINCT system, season FROM {table}").fetchall())  # noqa: S608
            except sqlite3.OperationalError:
                continue
    return [
        {"key": f"{system}-{season}", "label": f"{system} {season}", "system": system, "season": season}
        for system, season in sorted(found, key=lambda item: (-item[1], item[0].lower()))
    ]


def _value(row: sqlite3.Row, player_type: str, cat: str) -> float | None:
    columns = row.keys()

    def get(column: str) -> float | None:
        return row[column] if column in columns else None

    if cat == "W+QS":
        wins, quality_starts = get("w"), get("qs")
        return None if wins is None or quality_starts is None else wins + quality_starts
    if cat == "Kd9" and get("k9") is None:
        innings, strikeouts = get("ip"), get("so")
        return strikeouts * 9 / innings if innings and strikeouts is not None else None
    return get(COLUMNS[player_type].get(cat, cat.lower()))


def load_stats(system: str, season: int, player_type: str, db_path: str | Path | None = None) -> dict[str, dict]:
    """Player id -> {scoring category: projected value} from the system's latest snapshot."""
    path = zscores.stats_db_path(db_path)
    table = TABLES[player_type]
    categories = zscores.scoring_categories(player_type)
    if not path.exists():
        return {}
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE system = ? AND season = ? AND player_id IS NOT NULL "  # noqa: S608
                f"AND snapshot = (SELECT MAX(snapshot) FROM {table} WHERE system = ? AND season = ?)",
                (system, season, system, season),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
    return {row["player_id"]: {cat: _value(row, player_type, cat) for cat in categories} for row in rows}


def apply_stats(players: list, player_type: str, system: str, season: int, db_path: str | Path | None = None) -> None:
    """Replace each player's season-to-date scoring stats with the system's projection, in place.

    Players the system does not project (or that have no player id) are left with no stats.
    """
    projected = load_stats(system, season, player_type, db_path)
    for player in players:
        player.scoring_stats = projected.get(player.player_id, {})

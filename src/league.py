"""League teams and their latest standings, read from the stats database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

try:
    from . import zscores
except ImportError:  # pragma: no cover - supports direct module imports
    import zscores


def active_sgp_vr(conn: sqlite3.Connection) -> dict[int, float]:
    """Team id -> total SGP vs replacement of the players active in the team's latest lineup.

    Players are matched to the batting and pitching tables by fantasy team and player id; an
    active player without a score adds nothing. Teams without an imported lineup are absent.
    """
    lineups = zscores.latest_active_lineups(conn)
    if lineups is None:
        return {}
    totals = pd.Series(0.0, index=pd.Index(lineups["team_id"].unique(), name="team_id"))
    for player_type, (table, _columns) in zscores.TABLES.items():
        try:
            scores = pd.read_sql_query(
                f"SELECT team_id, player_id, sgp_total_above_replacement AS vr FROM {table} WHERE team_id IS NOT NULL AND player_id IS NOT NULL",  # noqa: S608
                conn,
            )
        except (sqlite3.OperationalError, pd.errors.DatabaseError):
            continue
        active = lineups[lineups["player_type"] == player_type][["team_id", "player_id"]]
        matched = active.astype({"team_id": "Int64"}).merge(scores.astype({"team_id": "Int64"}), on=["team_id", "player_id"])
        totals = totals.add(matched.groupby("team_id")["vr"].sum(), fill_value=0.0)
    return {int(team_id): float(total) for team_id, total in totals.items()}


def league_teams(db_path: str | Path | None = None) -> tuple[dict | None, list[dict]]:
    """Teams in the order of the most recent standings snapshot.

    Returns ``(snapshot, teams)``. ``snapshot`` is ``{"season_year", "as_of_date"}`` for the
    latest imported standings, or None if there are none. Each team has ``id``, ``name``,
    ``abbrev``, ``manager``, ``rank``, ``total_points`` (rank and points are None for a
    team missing from the snapshot, which is listed last) and ``active_sgp_vr`` (see
    ``active_sgp_vr``; None for a team without a lineup). Returns ``(None, [])`` if the
    database or its teams table is missing.
    """
    db_path = zscores.stats_db_path(db_path)
    if not db_path.exists():
        return None, []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            latest = conn.execute(
                "SELECT season_year, as_of_date FROM standings ORDER BY season_year DESC, as_of_date DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            latest = None
        snapshot = dict(latest) if latest else None
        standing_join = (
            "LEFT JOIN standings s ON s.team_id = t.id AND s.season_year = ? AND s.as_of_date = ?"
            if snapshot
            else "LEFT JOIN (SELECT NULL AS team_id, NULL AS rank, NULL AS total_points) s ON s.team_id = t.id"
        )
        try:
            rows = conn.execute(
                f"""
                SELECT t.id, t.name, t.abbrev, m.name AS manager, s.rank, s.total_points
                FROM teams t
                JOIN managers m ON m.id = t.manager_id
                {standing_join}
                ORDER BY s.rank IS NULL, s.rank, t.name
                """,
                (snapshot["season_year"], snapshot["as_of_date"]) if snapshot else (),
            ).fetchall()
        except sqlite3.OperationalError:
            return snapshot, []
        active = active_sgp_vr(conn)
    return snapshot, [dict(row, active_sgp_vr=active.get(row["id"])) for row in rows]


def get_team(team_id: int, db_path: str | Path | None = None) -> dict | None:
    """One team from ``league_teams`` by id, or None if it does not exist."""
    _snapshot, teams = league_teams(db_path)
    return next((team for team in teams if team["id"] == team_id), None)

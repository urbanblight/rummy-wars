"""League teams and their latest standings, read from the stats database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

try:
    from . import projections, zscores
except ImportError:  # pragma: no cover - supports direct module imports
    import projections
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


def projected_active_sgp_vr(conn: sqlite3.Connection) -> dict[str, dict[int, float]]:
    """System key (``ZIPS-2027``) -> team id -> total projected SGP vs replacement of the active players.

    Like ``active_sgp_vr``, but each player is worth the projection system's value for the
    coming season (see ``projections.save_projection_scores``). Systems without calculated
    scores are absent.
    """
    lineups = zscores.latest_active_lineups(conn)
    if lineups is None:
        return {}
    result: dict[str, pd.Series] = {}
    for player_type, table in projections.SCORE_TABLES.items():
        try:
            scores = pd.read_sql_query(
                f"SELECT system, season, player_id, sgp_total_above_replacement AS vr FROM {table} "  # noqa: S608
                "WHERE sgp_total_above_replacement IS NOT NULL",
                conn,
            )
        except (sqlite3.OperationalError, pd.errors.DatabaseError):
            continue
        active = lineups[lineups["player_type"] == player_type][["team_id", "player_id"]]
        merged = active.merge(scores, on="player_id")
        for (system, season), group in merged.groupby(["system", "season"]):
            totals = group.groupby("team_id")["vr"].sum()
            key = f"{system}-{season}"
            result[key] = totals.add(result[key], fill_value=0.0) if key in result else totals
    return {key: {int(team_id): float(total) for team_id, total in totals.items()} for key, totals in result.items()}


def league_teams(db_path: str | Path | None = None) -> tuple[dict | None, list[dict]]:
    """Teams in the order of the most recent standings snapshot.

    Returns ``(snapshot, teams)``. ``snapshot`` is ``{"season_year", "as_of_date"}`` for the
    latest imported standings, or None if there are none. Each team has ``id``, ``name``,
    ``abbrev``, ``manager``, ``rank``, ``total_points`` (rank and points are None for a
    team missing from the snapshot, which is listed last) and ``active_sgp_vr`` (see
    ``active_sgp_vr``; None for a team without a lineup) and ``projected_sgp_vr`` (system key ->
    ``projected_active_sgp_vr`` total). Returns ``(None, [])`` if the
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
        projected = projected_active_sgp_vr(conn)
    return snapshot, [
        dict(
            row,
            active_sgp_vr=active.get(row["id"]),
            projected_sgp_vr={key: totals.get(row["id"]) for key, totals in projected.items()},
        )
        for row in rows
    ]


def get_team(team_id: int, db_path: str | Path | None = None) -> dict | None:
    """One team from ``league_teams`` by id, or None if it does not exist."""
    _snapshot, teams = league_teams(db_path)
    return next((team for team in teams if team["id"] == team_id), None)

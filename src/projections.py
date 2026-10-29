"""Projected scoring stats from the batting and pitching projection tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

try:
    from . import zscores
except ImportError:  # pragma: no cover - supports direct module imports
    import zscores

TABLES = {"Batter": "batting_projections", "Pitcher": "pitching_projections"}
SCORE_TABLES = {"Batter": "batting_projection_scores", "Pitcher": "pitching_projection_scores"}
# Projection column -> the stats-table column the scoring code expects.
STATS_COLUMNS = {
    "Batter": {
        "ab": "AB", "r": "R", "h": "H", "singles": "1B", "doubles": "2B", "triples": "3B", "hr": "HR",
        "rbi": "RBI", "bb": "BB", "so": "K", "sb": "SB", "cs": "CS", "avg": "AVG", "obp": "OBP", "slg": "SLG",
    },
    "Pitcher": {
        "ip": "INNs", "w": "W", "l": "L", "sv": "S", "hld": "HD", "so": "K", "bb": "BB", "h": "H",
        "era": "ERA", "whip": "WHIP", "qs": "QS", "gs": "GS", "g": "APP",
    },
}
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
    """Replace each player's season-to-date stats and scores with the system's projection, in place.

    Players the system does not project (or that have no player id) are left with no stats or
    scores. The CBS rank is blanked because it describes the current season, not the projection.
    """
    projected = load_stats(system, season, player_type, db_path)
    scores = load_scores(system, season, player_type, db_path)
    stored = set(next(iter(scores.values())).keys()) if scores else set()
    categories = zscores.scoring_categories(player_type)
    context = zscores.ScoreContext(
        categories=categories,
        columns=zscores.TABLES[player_type][1],
        has_sgp=f"{zscores.sgp_column(zscores.TOTAL)}" in stored,
        has_z_repl=f"{zscores.z_column(zscores.TOTAL)}_{zscores.ABOVE_REPLACEMENT}" in stored,
        has_sgp_repl=f"{zscores.sgp_column(zscores.TOTAL)}_{zscores.ABOVE_REPLACEMENT}" in stored,
    )
    for player in players:
        player.scoring_stats = projected.get(player.player_id, {})
        player.cbs_rank = None
        for name in ("z_scores", "z_ranks", "sgp", "sgp_ranks", "z_above_replacement", "sgp_above_replacement"):
            setattr(player, name, {})
        player.z_total = player.z_total_rank = player.sgp_total = player.sgp_total_rank = None
        row = scores.get(player.player_id)
        if row is not None:
            zscores.attach_scores(player, row, context, stats=False)
            player.cbs_rank = None


def load_scores(system: str, season: int, player_type: str, db_path: str | Path | None = None) -> dict[str, sqlite3.Row]:
    """Player id -> stored score row for the system, or empty if scores were never calculated."""
    path = zscores.stats_db_path(db_path)
    if not path.exists():
        return {}
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT *, 0 AS rank FROM {SCORE_TABLES[player_type]} WHERE system = ? AND season = ?",  # noqa: S608
                (system, season),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
    return {row["player_id"]: row for row in rows}


def _latest_projections(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    """Every system's latest snapshot of a projection table."""
    return pd.read_sql_query(
        f"SELECT * FROM {table} p WHERE player_id IS NOT NULL AND snapshot = "  # noqa: S608 - fixed table names
        f"(SELECT MAX(snapshot) FROM {table} WHERE system = p.system AND season = p.season)",
        conn,
    )


def _score_system(
    conn: sqlite3.Connection, projected: pd.DataFrame, player_type: str, owners: pd.DataFrame, factors: dict
) -> pd.DataFrame:
    """Z-scores, SGP and above-replacement values for one system's projections of a player type.

    Ownership and positions come from the current stats table (players it does not list count
    as free agents), so replacement levels are the best players available now. There is no
    CBS rank for a projection, so the pool the z-scores and SGP are measured against is the
    top players by projected WAR.
    """
    df = projected.drop_duplicates(subset="player_id").rename(columns=STATS_COLUMNS[player_type])
    for column in STATS_COLUMNS[player_type].values():
        if column in df:
            df[column] = pd.to_numeric(df[column], errors="coerce")  # all-NULL columns load as object
    df["war"] = pd.to_numeric(df["war"], errors="coerce")
    df = df.merge(owners, on="player_id", how="left")
    df["team_id"] = df["team_id"].astype("Int64")
    df["positions"] = df["positions"].fillna("")
    df["rank"] = df["war"].rank(ascending=False, method="first").fillna(zscores.UNRANKED)
    df = zscores.score_frame(conn, df, player_type, factors)
    keep = ["system", "season", "snapshot", "player_id"]
    return df[keep + [c for c in df.columns if c.startswith(("z_", "sgp_"))]]


def save_projection_scores(db_path: str | Path | None = None) -> None:
    """Store z-scores, SGP, above-replacement values and ranks for each system's latest projections.

    Rebuilds ``batting_projection_scores`` and ``pitching_projection_scores``. Re-run after
    importing projections or new player stats, since replacement levels depend on both.
    """
    path = zscores.stats_db_path(db_path)
    if not path.exists():
        return
    with sqlite3.connect(path) as conn:
        factors = zscores.load_sgp_factors(conn)
        for player_type, table in TABLES.items():
            if not zscores.scoring_categories(player_type):
                continue
            stats_table = zscores.TABLES[player_type][0]
            try:
                projections = _latest_projections(conn, table)
                owners = pd.read_sql_query(
                    f"SELECT player_id, team_id, positions FROM {stats_table} "  # noqa: S608
                    "WHERE player_id IS NOT NULL ORDER BY rank",
                    conn,
                ).drop_duplicates(subset="player_id")
            except (sqlite3.OperationalError, pd.errors.DatabaseError):
                continue
            frames = [
                _score_system(conn, group, player_type, owners, factors)
                for _key, group in projections.groupby(["system", "season"])
            ]
            if frames:
                pd.concat(frames).to_sql(SCORE_TABLES[player_type], conn, if_exists="replace", index=False)

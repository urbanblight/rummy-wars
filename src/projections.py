"""Projected scoring stats from the batting and pitching projection tables."""

from __future__ import annotations

import pandas as pd

try:
    from . import db, zscores
except ImportError:  # pragma: no cover - supports direct module imports
    import db
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


def systems() -> list[dict]:
    """Projection systems with imported data, as ``{"key", "label", "system", "season"}``.

    ``key`` (``ZIPS-2027``) is what the stats source selector submits. Newest season first.
    """
    conn = db.connect_or_none()
    if conn is None:
        return []
    found: set[tuple[str, int]] = set()
    with conn:
        for table in TABLES.values():
            try:
                found.update(tuple(row) for row in conn.execute(f"SELECT DISTINCT `system`, season FROM {table}").fetchall())  # noqa: S608
            except db.SchemaError:
                continue
    return [
        {"key": f"{system}-{season}", "label": f"{system} {season}", "system": system, "season": season}
        for system, season in sorted(found, key=lambda item: (-item[1], item[0].lower()))
    ]


def _value(row: db.Row, player_type: str, cat: str) -> float | None:
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


def load_stats(system: str, season: int, player_type: str) -> dict[str, dict]:
    """Player id -> {scoring category: projected value} from the system's latest snapshot."""
    table = TABLES[player_type]
    categories = zscores.scoring_categories(player_type)
    conn = db.connect_or_none()
    if conn is None:
        return {}
    with conn:
        try:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE `system` = %s AND season = %s AND player_id IS NOT NULL "  # noqa: S608
                f"AND snapshot = (SELECT MAX(snapshot) FROM {table} WHERE `system` = %s AND season = %s)",
                (system, season, system, season),
            ).fetchall()
        except db.SchemaError:
            return {}
    return {row["player_id"]: {cat: _value(row, player_type, cat) for cat in categories} for row in rows}


def apply_stats(players: list, player_type: str, system: str, season: int) -> None:
    """Replace each player's season-to-date stats and scores with the system's projection, in place.

    Players the system does not project (or that have no player id) are left with no stats or
    scores. The CBS rank is blanked because it describes the current season, not the projection.
    """
    projected = load_stats(system, season, player_type)
    scores = load_scores(system, season, player_type)
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


def load_scores(system: str, season: int, player_type: str) -> dict[str, db.Row]:
    """Player id -> stored score row for the system, or empty if scores were never calculated."""
    conn = db.connect_or_none()
    if conn is None:
        return {}
    with conn:
        try:
            rows = conn.execute(
                f"SELECT *, 0 AS `rank` FROM {SCORE_TABLES[player_type]} WHERE `system` = %s AND season = %s",  # noqa: S608
                (system, season),
            ).fetchall()
        except db.SchemaError:
            return {}
    return {row["player_id"]: row for row in rows}


def _latest_projections(conn: db.Connection, table: str) -> pd.DataFrame:
    """Every system's latest snapshot of a projection table."""
    return conn.read_sql(
        f"SELECT * FROM {table} p WHERE player_id IS NOT NULL AND snapshot = "  # noqa: S608 - fixed table names
        f"(SELECT MAX(snapshot) FROM {table} WHERE `system` = p.`system` AND season = p.season)",
    )


def _score_system(
    conn: db.Connection, projected: pd.DataFrame, player_type: str, owners: pd.DataFrame, factors: dict
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


def save_projection_scores() -> None:
    """Store z-scores, SGP, above-replacement values and ranks for each system's latest projections.

    Rebuilds ``batting_projection_scores`` and ``pitching_projection_scores``. Re-run after
    importing projections or new player stats, since replacement levels depend on both.
    """
    with db.connect() as conn:
        factors = zscores.load_sgp_factors(conn)
        for player_type, table in TABLES.items():
            if not zscores.scoring_categories(player_type):
                continue
            stats_table = zscores.TABLES[player_type][0]
            try:
                projections = _latest_projections(conn, table)
                owners = conn.read_sql(
                    f"SELECT player_id, team_id, positions FROM {stats_table} "  # noqa: S608
                    "WHERE player_id IS NOT NULL ORDER BY `rank`",
                ).drop_duplicates(subset="player_id")
            except (*db.SchemaError, pd.errors.DatabaseError):
                continue
            frames = [
                _score_system(conn, group, player_type, owners, factors)
                for _key, group in projections.groupby(["system", "season"])
            ]
            if frames:
                conn.write_frame(pd.concat(frames), SCORE_TABLES[player_type])

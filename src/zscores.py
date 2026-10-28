"""League z-score evaluation of rostered players against the top of the player pool."""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from pathlib import Path

import pandas as pd

try:
    from . import models
except ImportError:  # pragma: no cover - supports direct module imports
    import models

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "stats.db"
POOL_SIZE = 500
UNRANKED = 9999

# Scoring category -> how to derive it from the imported table's columns.
BATTING_COLUMNS = {"BA": "AVG"}
PITCHING_COLUMNS = {"SV": "S", "INN": "INNs"}
# Categories where a lower value is better, so the z-score is inverted.
LOWER_IS_BETTER = {"ERA", "WHIP"}
# Ratio categories are weighted by playing time (AB for batters, innings for pitchers).
RATE_CATEGORIES = {"BA", "OBP", "SLG", "ERA", "WHIP", "K/9"}


def scoring_categories(player_type: str) -> list[str]:
    prefix = "BATTING" if player_type == "Batter" else "PITCHING"
    raw = os.getenv(f"{prefix}_SCORING_STATS") or os.getenv(f"{prefix}_SCORING_CATEGORIES", "")
    return [cat.strip() for cat in raw.split(",") if cat.strip()]


def _add_category(df: pd.DataFrame, cat: str, columns: dict[str, str]) -> pd.Series:
    if cat == "W+QS":
        return df["W"] + df["QS"]
    if cat == "K/9":
        return df["K"] * 9 / df["INNs"].where(df["INNs"] > 0)
    return df[columns.get(cat, cat)]


def z_column(cat: str) -> str:
    """Sqlite-safe column name holding a category's z-score, e.g. ``K/9`` -> ``z_K_9``."""
    return "z_" + re.sub(r"[^A-Za-z0-9]+", "_", cat).strip("_")


TOTAL = "total"
PLAYING_TIME_COLUMN = {"Batter": "AB", "Pitcher": "INNs"}
TABLES = {
    "Batter": ("batting", BATTING_COLUMNS),
    "Pitcher": ("pitching", PITCHING_COLUMNS),
}


def _weight_by_volume(z: pd.Series, volume: pd.Series) -> pd.Series:
    """Scale ratio z-scores so that less playing time never ranks a player higher.

    ``volume`` is playing time relative to the pool average. Good (positive)
    z-scores scale up or down with volume. Bad (negative) z-scores are amplified
    up to 2x as volume falls below average, so a few bad AB don't look better
    than a full season of mediocre production.
    """
    penalty = 2 - volume.clip(upper=1)
    return z.where(z < 0, z * volume).where(z >= 0, z * penalty)


def compute_z_scores(
    df: pd.DataFrame, categories: list[str], columns: dict[str, str], playing_time: str | None = None
) -> pd.DataFrame:
    """Add ``z_<cat>``, ``z_total`` and matching ``*_rank`` columns to a copy of ``df``.

    Mean and standard deviation come from the top ``POOL_SIZE`` ranked players.
    Ratio categories (``RATE_CATEGORIES``) are multiplied by the player's playing
    time relative to the pool average (see ``_weight_by_volume``), so a .300 hitter
    over 600 AB counts for more than one over 30 AB. Players without playing time (``playing_time`` column <= 0) get no z-scores
    and are left out of the ranks. Ranks are 1 = best, within the whole table.
    """
    df = df.copy()
    pool = df[df["rank"] < UNRANKED].nsmallest(POOL_SIZE, "rank")
    z_columns = []
    for cat in categories:
        values = _add_category(df, cat, columns)
        pool_values = values.loc[pool.index]
        std = pool_values.std()
        z = (values - pool_values.mean()) / std if std else values * 0.0
        z = -z if cat in LOWER_IS_BETTER else z
        if playing_time and cat in RATE_CATEGORIES:
            z = _weight_by_volume(z, df[playing_time] / df.loc[pool.index, playing_time].mean())
        df[z_column(cat)] = z
        z_columns.append(z_column(cat))
    df[z_column(TOTAL)] = df[z_columns].sum(axis=1)
    all_columns = z_columns + [z_column(TOTAL)]
    if playing_time:
        df.loc[df[playing_time] <= 0, all_columns] = float("nan")
    for column in all_columns:
        df[f"{column}_rank"] = df[column].rank(ascending=False, method="min")
    return df


def save_z_scores(db_path: str | Path) -> None:
    """Compute and store z-scores and ranks in the batting and pitching tables."""
    with sqlite3.connect(db_path) as conn:
        for player_type, (table, columns) in TABLES.items():
            categories = scoring_categories(player_type)
            if not categories:
                continue
            df = pd.read_sql_query(f"SELECT * FROM {table}", conn)  # noqa: S608 - fixed table names
            stale = [c for c in df.columns if c.startswith("z_")]
            df = compute_z_scores(df.drop(columns=stale), categories, columns, PLAYING_TIME_COLUMN[player_type])
            if "team_id" in df:
                df["team_id"] = df["team_id"].astype("Int64")  # keep integers despite NULLs
            df.to_sql(table, conn, if_exists="replace", index=False)


def normalize_name(name: str) -> str:
    """Lowercase, accent-free, punctuation-free name used to match players."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", ascii_name.lower().replace("-", " ")).strip()


def evaluate_roster(roster: models.TeamRoster, db_path: str | Path | None = None) -> None:
    """Attach the stored z-scores and ranks to each rostered player in place.

    Reads the columns written by ``save_z_scores``; players missing from the
    stats pool (or without playing time) are left without z-scores.
    """
    db_path = Path(db_path or os.getenv("STATS_DB") or DEFAULT_DB)
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for player_type, (table, _columns) in TABLES.items():
            categories = scoring_categories(player_type)
            players = [p for p in roster.players if p.player_type == player_type]
            if not categories or not players:
                continue
            wanted = [TOTAL, *categories]
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - fixed table names
            except sqlite3.OperationalError:
                continue
            if rows and any(z_column(c) not in rows[0].keys() for c in wanted):
                continue  # DB was imported with different scoring categories
            by_name: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_name.setdefault(row["norm_name"], []).append(row)
            for player in players:
                matches = by_name.get(normalize_name(player.name), [])
                same_team = [r for r in matches if r["team"] == player.team]
                matches = same_team or matches
                if not matches or matches[0][z_column(TOTAL)] is None:
                    continue
                row = matches[0]
                player.z_scores = {cat: row[z_column(cat)] for cat in categories}
                player.z_ranks = {cat: int(row[f"{z_column(cat)}_rank"]) for cat in categories}
                player.z_total = row[z_column(TOTAL)]
                player.z_total_rank = int(row[f"{z_column(TOTAL)}_rank"])


def team_totals(db_path: str | Path | None = None) -> list[dict]:
    """Total z-score per fantasy team (batters, pitchers and combined), best first.

    Sums the stored ``z_total`` of every player a team owns. Returns an empty
    list if the stats DB or its teams table has not been imported.
    """
    db_path = Path(db_path or os.getenv("STATS_DB") or DEFAULT_DB)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT t.id, t.name, t.abbrev, t.manager,
                       COALESCE(b.z, 0) AS batting_z, COALESCE(b.n, 0) AS batters,
                       COALESCE(p.z, 0) AS pitching_z, COALESCE(p.n, 0) AS pitchers
                FROM teams t
                LEFT JOIN (SELECT team_id, SUM(z_total) AS z, COUNT(z_total) AS n
                           FROM batting GROUP BY team_id) b ON b.team_id = t.id
                LEFT JOIN (SELECT team_id, SUM(z_total) AS z, COUNT(z_total) AS n
                           FROM pitching GROUP BY team_id) p ON p.team_id = t.id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    teams = [dict(row, total_z=row["batting_z"] + row["pitching_z"]) for row in rows]
    return sorted(teams, key=lambda team: team["total_z"], reverse=True)

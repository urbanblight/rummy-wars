"""League z-score and SGP evaluation of rostered players against the top of the player pool."""

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
DEFAULT_ACTIVE_SLOTS = 10
# Replacement level at a position is the average of this many best free agents there.
REPLACEMENT_POOL = 5
PITCHER_POSITIONS = {"SP", "RP"}
ABOVE_REPLACEMENT = "above_replacement"

# Scoring category -> how to derive it from the imported table's columns.
BATTING_COLUMNS = {"BA": "AVG"}
PITCHING_COLUMNS = {"INN": "INNs"}
# Categories where a lower value is better, so the z-score is inverted.
LOWER_IS_BETTER = {"ERA", "WHIP"}
# Ratio categories are weighted by playing time (AB for batters, innings for pitchers).
RATE_CATEGORIES = {"BA", "OBP", "SLG", "ERA", "WHIP", "Kd9"}


def scoring_categories(player_type: str) -> list[str]:
    prefix = "BATTING" if player_type == "Batter" else "PITCHING"
    raw = os.getenv(f"{prefix}_SCORING_STATS") or os.getenv(f"{prefix}_SCORING_CATEGORIES", "")
    return [cat.strip() for cat in raw.split(",") if cat.strip()]


def _add_category(df: pd.DataFrame, cat: str, columns: dict[str, str]) -> pd.Series:
    if cat == "W+QS":
        return df["W"] + df["QS"]
    if cat == "Kd9":
        return df["K"] * 9 / df["INNs"].where(df["INNs"] > 0)
    return df[columns.get(cat, cat)]


def _slug(cat: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", cat).strip("_")


def z_column(cat: str) -> str:
    """Sqlite-safe column name holding a category's z-score, e.g. ``W+QS`` -> ``z_W_QS``."""
    return "z_" + _slug(cat)


def sgp_column(cat: str) -> str:
    """Sqlite-safe column name holding a category's SGP, e.g. ``W+QS`` -> ``sgp_W_QS``."""
    return "sgp_" + _slug(cat)


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

    ``z_total`` is the average of the category z-scores, so it stays on the same
    scale however many categories the league scores.

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
    df[z_column(TOTAL)] = df[z_columns].mean(axis=1)
    all_columns = z_columns + [z_column(TOTAL)]
    if playing_time:
        df.loc[df[playing_time] <= 0, all_columns] = float("nan")
    for column in all_columns:
        df[f"{column}_rank"] = df[column].rank(ascending=False, method="min")
    return df


def load_sgp_factors(conn: sqlite3.Connection) -> dict[str, float]:
    """Category -> stat per standings point from ``sgp_factors`` (empty if not calculated)."""
    try:
        return dict(conn.execute("SELECT category, factor FROM sgp_factors").fetchall())
    except sqlite3.OperationalError:
        return {}


def active_slots(player_type: str) -> int:
    """Hitter or pitcher slots a team counts each day (``BATTING_ACTIVE_SLOTS`` / ``PITCHING_ACTIVE_SLOTS``)."""
    prefix = "BATTING" if player_type == "Batter" else "PITCHING"
    return int(os.getenv(f"{prefix}_ACTIVE_SLOTS", DEFAULT_ACTIVE_SLOTS))


def lineup_volume(df: pd.DataFrame, playing_time: str, slots: int) -> float:
    """Average, across teams, of the playing time of the ``slots`` most-used players each team owns.

    Bench, reserve and minors players never count toward a team's totals, so only
    a lineup's worth of players is summed.
    """
    owned = df[df["team_id"].notna()]
    top = owned.sort_values(playing_time, ascending=False).groupby("team_id").head(slots)
    teams = top["team_id"].nunique()
    return top[playing_time].sum() / teams if teams else 0.0


def standings_innings(conn: sqlite3.Connection) -> float | None:
    """Average innings per team in the latest full-year standings, or None if none are imported."""
    row = conn.execute(
        """
        SELECT AVG(value) FROM standings_categories
        WHERE category = 'INN' AND as_of_date = season_year || '-12-31'
          AND season_year = (SELECT MAX(season_year) FROM standings_categories
                             WHERE category = 'INN' AND as_of_date = season_year || '-12-31')
        """
    ).fetchone()
    return row[0] if row else None


def team_volume(conn: sqlite3.Connection, df: pd.DataFrame, player_type: str) -> float:
    """Typical team playing time: real team innings for pitchers, a lineup's AB for batters."""
    playing_time = PLAYING_TIME_COLUMN[player_type]
    if player_type == "Pitcher":
        try:
            innings = standings_innings(conn)
        except sqlite3.OperationalError:
            innings = None
        if innings:
            return innings
    return lineup_volume(df, playing_time, active_slots(player_type))


def compute_sgp(
    df: pd.DataFrame,
    categories: list[str],
    columns: dict[str, str],
    factors: dict[str, float],
    playing_time: str,
    volume_per_team: float,
) -> pd.DataFrame:
    """Add ``sgp_<cat>``, ``sgp_total`` and matching ``*_rank`` columns to a copy of ``df``.

    SGP is the number of standings points a player is worth in a category:
    (value - pool mean) / factor, where the pool mean comes from the top
    ``POOL_SIZE`` ranked players and ``factor`` is the stat per standings point
    from ``sgp_factors``. Ratio categories (``RATE_CATEGORIES``) are first scaled
    by the player's share of a typical team's volume, since a rate only moves a
    team's total in proportion to the playing time behind it. Lower-is-better
    categories are inverted. Players without playing time get no SGP and are
    left out of the ranks. Returns ``df`` unchanged if a factor is missing.
    """
    df = df.copy()
    if volume_per_team <= 0 or any(factors.get(cat, 0) <= 0 for cat in categories):
        return df
    pool = df[df["rank"] < UNRANKED].nsmallest(POOL_SIZE, "rank")
    sgp_columns = []
    for cat in categories:
        values = _add_category(df, cat, columns)
        gain = values - values.loc[pool.index].mean()
        gain = -gain if cat in LOWER_IS_BETTER else gain
        if cat in RATE_CATEGORIES:
            gain = gain * df[playing_time] / volume_per_team
        df[sgp_column(cat)] = gain / factors[cat]
        sgp_columns.append(sgp_column(cat))
    df[sgp_column(TOTAL)] = df[sgp_columns].sum(axis=1)
    all_columns = sgp_columns + [sgp_column(TOTAL)]
    df.loc[df[playing_time] <= 0, all_columns] = float("nan")
    for column in all_columns:
        df[f"{column}_rank"] = df[column].rank(ascending=False, method="min")
    return df


def eligible_positions(positions: str | None, player_type: str) -> list[str]:
    """Positions a player qualifies at, from CBS's comma-separated list (DH counts as UT).

    Pitchers only qualify at SP and RP (a two-way player's DH eligibility is ignored).
    LF, CF and RF stay separate positions.
    """
    found = [p.strip() for p in (positions or "").split(",") if p.strip()]
    found = ["UT" if p == "DH" else p for p in found]
    if player_type == "Pitcher":
        found = [p for p in found if p in PITCHER_POSITIONS]
    return list(dict.fromkeys(found))


def replacement_levels(df: pd.DataFrame, column: str, eligible: pd.Series) -> dict[str, float]:
    """Position -> average ``column`` of the best ``REPLACEMENT_POOL`` free agents eligible there.

    Free agents are players without a ``team_id``; those without playing time (NaN) are ignored.
    """
    free = df["team_id"].isna() & df[column].notna()
    pool = pd.DataFrame({"position": eligible[free], "value": df.loc[free, column]}).explode("position")
    pool = pool.dropna(subset=["position"])
    return {
        position: group["value"].nlargest(REPLACEMENT_POOL).mean()
        for position, group in pool.groupby("position")
    }


def compute_above_replacement(df: pd.DataFrame, player_type: str, columns: list[str]) -> pd.DataFrame:
    """Add ``<column>_above_replacement`` and its ``_rank`` for each score column in ``columns``.

    Each column (a category or a total) gets its own replacement level per position.

    A player is measured against the lowest replacement level among the positions
    he is eligible at, i.e. where he adds the most value over a free agent.
    Players at a position with no free agents, or without a score, get NaN.
    """
    df = df.copy()
    eligible = df["positions"].map(lambda value: eligible_positions(value, player_type))
    for column in columns:
        levels = replacement_levels(df, column, eligible)
        baseline = eligible.map(
            lambda positions: min((levels[p] for p in positions if p in levels), default=float("nan"))
        )
        name = f"{column}_{ABOVE_REPLACEMENT}"
        df[name] = df[column] - baseline
        df[f"{name}_rank"] = df[name].rank(ascending=False, method="min")
    return df


def save_z_scores(db_path: str | Path) -> None:
    """Compute and store z-scores, SGP (when factors exist), above-replacement values and ranks in the batting and pitching tables."""
    with sqlite3.connect(db_path) as conn:
        factors = load_sgp_factors(conn)
        for player_type, (table, columns) in TABLES.items():
            categories = scoring_categories(player_type)
            if not categories:
                continue
            df = pd.read_sql_query(f"SELECT * FROM {table}", conn)  # noqa: S608 - fixed table names
            stale = [c for c in df.columns if c.startswith(("z_", "sgp_"))]
            playing_time = PLAYING_TIME_COLUMN[player_type]
            df = compute_z_scores(df.drop(columns=stale), categories, columns, playing_time)
            if factors and "team_id" in df:
                df = compute_sgp(df, categories, columns, factors, playing_time, team_volume(conn, df, player_type))
            if "team_id" in df and "positions" in df:
                scored = [
                    column(cat) for column in (z_column, sgp_column) for cat in (*categories, TOTAL)
                    if column(cat) in df
                ]
                df = compute_above_replacement(df, player_type, scored)
            if "team_id" in df:
                df["team_id"] = df["team_id"].astype("Int64")  # keep integers despite NULLs
            df.to_sql(table, conn, if_exists="replace", index=False)


def normalize_name(name: str) -> str:
    """Lowercase, accent-free, punctuation-free name used to match players."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", ascii_name.lower().replace("-", " ")).strip()


def scoring_stat(row: sqlite3.Row, cat: str, columns: dict[str, str]) -> float | None:
    """A player's raw value in one scoring category, derived like ``_add_category``."""
    if cat == "W+QS":
        wins, quality_starts = row["W"], row["QS"]
        return None if wins is None or quality_starts is None else wins + quality_starts
    if cat == "Kd9":
        innings, strikeouts = row["INNs"], row["K"]
        return strikeouts * 9 / innings if innings and strikeouts is not None else None
    return row[columns.get(cat, cat)]


def _above_replacement(row: sqlite3.Row, column, categories: list[str]) -> dict:
    """Total and per-category above-replacement values with ranks, as ``{key: (value, rank)}``."""
    result = {}
    for key in (TOTAL, *categories):
        name = f"{column(key)}_{ABOVE_REPLACEMENT}"
        value, rank = row[name], row[f"{name}_rank"]
        result[key] = (value, int(rank)) if value is not None and rank is not None else None
    return result


def evaluate_roster(roster: models.TeamRoster, db_path: str | Path | None = None) -> None:
    """Attach the stored z-scores, SGP and ranks to each rostered player in place.

    Reads the columns written by ``save_z_scores``; players missing from the
    stats pool (or without playing time) are left without scores. SGP is
    attached only when the database has it.
    """
    db_path = Path(db_path or os.getenv("STATS_DB") or DEFAULT_DB)
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for player_type, (table, columns) in TABLES.items():
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
            has_sgp = bool(rows) and all(sgp_column(c) in rows[0].keys() for c in wanted)
            stored = set(rows[0].keys()) if rows else set()
            has_z_repl, has_sgp_repl = (
                all(f"{column(cat)}_{ABOVE_REPLACEMENT}" in stored for cat in wanted)
                for column in (z_column, sgp_column)
            )
            by_name: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_name.setdefault(row["norm_name"], []).append(row)
            for player in players:
                matches = by_name.get(normalize_name(player.name), [])
                same_team = [r for r in matches if r["team"] == player.team]
                matches = same_team or matches
                if not matches:
                    continue
                row = matches[0]
                player.scoring_stats = {cat: scoring_stat(row, cat, columns) for cat in categories}
                if row[z_column(TOTAL)] is None:
                    continue
                player.z_scores = {cat: row[z_column(cat)] for cat in categories}
                player.z_ranks = {cat: int(row[f"{z_column(cat)}_rank"]) for cat in categories}
                player.z_total = row[z_column(TOTAL)]
                player.z_total_rank = int(row[f"{z_column(TOTAL)}_rank"])
                if has_sgp and row[sgp_column(TOTAL)] is not None:
                    player.sgp = {cat: row[sgp_column(cat)] for cat in categories}
                    player.sgp_ranks = {cat: int(row[f"{sgp_column(cat)}_rank"]) for cat in categories}
                    player.sgp_total = row[sgp_column(TOTAL)]
                    player.sgp_total_rank = int(row[f"{sgp_column(TOTAL)}_rank"])
                if has_z_repl and row[f"{z_column(TOTAL)}_{ABOVE_REPLACEMENT}"] is not None:
                    player.z_above_replacement = _above_replacement(row, z_column, categories)
                if has_sgp_repl and row[f"{sgp_column(TOTAL)}_{ABOVE_REPLACEMENT}"] is not None:
                    player.sgp_above_replacement = _above_replacement(row, sgp_column, categories)

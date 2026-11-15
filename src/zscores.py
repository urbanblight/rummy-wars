"""League z-score and SGP evaluation of rostered players against the top of the player pool.

Two complementary measures are computed per scoring category and in total:

* **z-scores** compare a player with the mean and spread of the top ``POOL_SIZE`` ranked
  players, so every category lands on one comparable scale.
* **SGP** (standings gain points) converts the same gap into standings points using the
  stat-per-point factors in ``sgp_factors``.

Both are also expressed *above replacement*: relative to what a freely available player
(or, for pitchers, the depth of the active pool) would provide at the same position.

The module has three layers:

1. Pure pandas computation (``compute_z_scores``, ``compute_sgp``,
   ``compute_above_replacement``, ``score_frame``) that works on a stats table.
2. ``save_z_scores``, which runs that computation and writes the results back to the
   ``batting`` and ``pitching`` tables so the web app only has to read them.
3. Read-side helpers (``evaluate_roster``, ``load_players``, ``load_free_agents``, ...) and
   small TTL caches of id mappings and rankings used when rendering pages.

Database access goes through :mod:`db`; the read helpers degrade to empty results when the
database or a table is missing so pages still render.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pandas as pd

try:
    from . import db, models
    from .player_ids import PlayerIndex, normalize_name
except ImportError:  # pragma: no cover - supports direct module imports
    import db
    import models
    from player_ids import PlayerIndex, normalize_name

# Size of the reference pool: only the best ``POOL_SIZE`` ranked players define the mean and
# spread, so the long tail of marginal players does not drag the scale down.
POOL_SIZE = 250
# Rank CBS assigns to players it leaves unranked; such players never enter the pool.
UNRANKED = 9999
# Active hitter/pitcher slots per team when ``*_ACTIVE_SLOTS`` is not configured.
DEFAULT_ACTIVE_SLOTS = 10
# Replacement level at a position is the average of this many best free agents there.
REPLACEMENT_POOL = 5
# Free agents need at least this much playing time (AB or innings) to count toward replacement level.
MIN_REPLACEMENT_VOLUME = {"Batter": 100, "Pitcher": 30}
PITCHER_POSITIONS = {"SP", "RP"}
# A pitcher who starts at least this share of his appearances is a starter, otherwise a reliever.
STARTER_SHARE = 0.5
# Column-name infix for above-replacement values, e.g. ``z_total_above_replacement``.
ABOVE_REPLACEMENT = "above_replacement"

# Scoring category -> how to derive it from the imported table's columns.
BATTING_COLUMNS = {"BA": "AVG"}
PITCHING_COLUMNS = {"INN": "INNs"}
# Categories where a lower value is better, so the z-score is inverted.
LOWER_IS_BETTER = {"ERA", "WHIP"}
# Ratio categories are weighted by playing time (AB for batters, innings for pitchers).
RATE_CATEGORIES = {"BA", "OBP", "SLG", "ERA", "WHIP", "Kd9"}


def scoring_categories(player_type: str) -> list[str]:
    """The league's scoring categories for a player type, in configured order.

    Read from ``BATTING_SCORING_STATS`` / ``PITCHING_SCORING_STATS`` (preferred) or the older
    ``*_SCORING_CATEGORIES`` variable, as comma-separated names. Reading the environment on
    every call keeps the result in step with configuration changes (and tests).

    Args:
        player_type: ``"Batter"`` or ``"Pitcher"``.

    Returns:
        The category names, empty when none are configured.
    """
    prefix = "BATTING" if player_type == "Batter" else "PITCHING"
    raw = os.getenv(f"{prefix}_SCORING_STATS") or os.getenv(f"{prefix}_SCORING_CATEGORIES", "")
    return [cat.strip() for cat in raw.split(",") if cat.strip()]


def _add_category(df: pd.DataFrame, cat: str, columns: dict[str, str]) -> pd.Series:
    """Raw values of a scoring category for every row of ``df``.

    Most categories are a column (possibly renamed through ``columns``); ``W+QS`` and ``Kd9``
    are derived. Innings of zero give NaN for ``Kd9`` rather than a division error.
    """
    if cat == "W+QS":
        return df["W"] + df["QS"]
    if cat == "Kd9":
        return df["K"] * 9 / df["INNs"].where(df["INNs"] > 0)
    return df[columns.get(cat, cat)]


def _slug(cat: str) -> str:
    """Make a category name safe inside a SQL column name (``W+QS`` -> ``W_QS``)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", cat).strip("_")


def z_column(cat: str) -> str:
    """Plain-identifier column name holding a category's z-score, e.g. ``W+QS`` -> ``z_W_QS``."""
    return "z_" + _slug(cat)


def sgp_column(cat: str) -> str:
    """Plain-identifier column name holding a category's SGP, e.g. ``W+QS`` -> ``sgp_W_QS``."""
    return "sgp_" + _slug(cat)


# Pseudo-category naming the combined score (``z_total``, ``sgp_total`` ...).
TOTAL = "total"
# Stats column holding a player type's playing time.
PLAYING_TIME_COLUMN = {"Batter": "AB", "Pitcher": "INNs"}
# Player type -> (stats table, category -> column renames for that table).
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

    Args:
        df: Stats table with a ``rank`` column (CBS rank) and the category columns.
        categories: Scoring categories to score.
        columns: Category -> column renames (see ``TABLES``).
        playing_time: Playing-time column (``AB`` / ``INNs``); enables volume weighting.

    Returns:
        A copy of ``df`` with the new columns.

    ``z_total`` is the average of the category z-scores, so it stays on the same
    scale however many categories the league scores.

    Mean and standard deviation come from the top ``POOL_SIZE`` ranked players.
    Ratio categories (``RATE_CATEGORIES``) are multiplied by the player's playing
    time relative to the pool average (see ``_weight_by_volume``), so a .300 hitter
    over 600 AB counts for more than one over 30 AB. Players without playing time
    (``playing_time`` column <= 0) get no z-scores and are left out of the ranks. Ranks
    are 1 = best, within the whole table. A category with no spread in the pool scores 0
    for everyone instead of dividing by zero.
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


def load_sgp_factors(conn: db.Connection) -> dict[str, float]:
    """Category -> stat per standings point from ``sgp_factors`` (empty if not calculated).

    A missing table is normal before ``scripts/calculate_sgp.py`` has run, so it is not an error.
    """
    try:
        return dict(conn.execute("SELECT category, factor FROM sgp_factors").fetchall())
    except db.SchemaError:
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


def standings_innings(conn: db.Connection) -> float | None:
    """Average innings per team in the latest full-year standings, or None if none are imported."""
    row = conn.execute(
        """
        SELECT AVG(value) FROM standings_categories
        WHERE category = 'INN' AND as_of_date = CONCAT(season_year, '-12-31')
          AND season_year = (SELECT MAX(season_year) FROM standings_categories
                             WHERE category = 'INN' AND as_of_date = CONCAT(season_year, '-12-31'))
        """
    ).fetchone()
    return row[0] if row else None


def latest_active_lineups(conn: db.Connection) -> pd.DataFrame | None:
    """Players in each team's active slots in its most recent imported lineup, or None.

    Columns: ``team_id``, ``player_id``, ``player_type`` and ``slot``. Needs the ``lineups``
    table from ``scripts/import_lineups.py``.
    """
    try:
        lineups = conn.read_sql(
            """
            SELECT l.team_id, l.player_id, l.player_type, l.slot
            FROM lineups l
            JOIN (SELECT team_id, MAX(CONCAT(effective_date, LPAD(IFNULL(period_id, 0), 6, '0'))) AS latest
                  FROM lineups GROUP BY team_id) m
              ON m.team_id = l.team_id
             AND CONCAT(l.effective_date, LPAD(IFNULL(l.period_id, 0), 6, '0')) = m.latest
            WHERE l.status = 'Active' AND l.player_id IS NOT NULL
            """
        )
    except (*db.SchemaError, pd.errors.DatabaseError):
        return None
    return lineups if not lineups.empty else None


def lineup_slot_counts(lineups: pd.DataFrame | None, player_type: str) -> dict[str, float]:
    """Average active slots per team by slot name (``C``, ``DH``, ``SP``...), from real lineups."""
    if lineups is None:
        return {}
    teams = lineups["team_id"].nunique()
    counts = lineups[lineups["player_type"] == player_type].groupby("slot").size()
    return {slot: count / teams for slot, count in counts.items()}


def active_lineup_volume(df: pd.DataFrame, lineups: pd.DataFrame, player_type: str) -> float | None:
    """Average, across teams, of the playing time of the players in a team's active slots.

    Players are matched to the stats table by fantasy team and player id. Returns None when
    no active player of that type can be matched.
    """
    playing_time = PLAYING_TIME_COLUMN[player_type]
    active = lineups[lineups["player_type"] == player_type][["team_id", "player_id"]].astype({"team_id": "Int64"})
    if active.empty:
        return None
    stats = df[df["team_id"].notna() & df["player_id"].notna()][["team_id", "player_id", playing_time]].astype({"team_id": "Int64"})
    matched = active.merge(stats, on=["team_id", "player_id"])
    if matched.empty:
        return None
    return float(matched.groupby("team_id")[playing_time].sum().mean())


def roster_coverage(conn: db.Connection, lineups: pd.DataFrame) -> float:
    """How much of a team's season innings its final active lineup accounts for, as a multiplier.

    The lineups are a snapshot, so players who threw innings earlier in the season for the
    team but are no longer active are missing. The real team innings from the standings,
    divided by the active pitchers' innings, measures that; it is at least 1.
    """
    try:
        innings = standings_innings(conn)
        pitching = conn.read_sql("SELECT team_id, player_id, INNs FROM pitching")
    except (*db.SchemaError, pd.errors.DatabaseError):
        return 1.0
    active = active_lineup_volume(pitching, lineups, "Pitcher")
    return max(innings / active, 1.0) if innings and active else 1.0


def team_volume(conn: db.Connection, df: pd.DataFrame, player_type: str) -> float:
    """Typical team playing time (AB or innings), from the best source available.

    Pitchers use the real team innings in the standings. Otherwise, and for batters, the
    active lineups are used: the playing time of the players in a team's active slots,
    scaled for batters by ``roster_coverage`` because a lineup is a snapshot. Without
    lineups, a team's most-used players stand in for its active slots.

    Args:
        conn: Open database connection.
        df: Stats table with ``team_id``, ``player_id`` and the playing-time column.
        player_type: ``"Batter"`` or ``"Pitcher"``.

    Returns:
        Playing time (AB or innings) of a typical team; 0.0 if no team owns anyone.
    """
    playing_time = PLAYING_TIME_COLUMN[player_type]
    if player_type == "Pitcher":
        try:
            innings = standings_innings(conn)
        except db.SchemaError:
            innings = None
        if innings:
            return innings
    lineups = latest_active_lineups(conn)
    if lineups is not None:
        volume = active_lineup_volume(df, lineups, player_type)
        if volume:
            return volume * roster_coverage(conn, lineups) if player_type == "Batter" else volume
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

    Args:
        df: Stats table with ``rank`` and the category columns.
        categories: Scoring categories to score.
        columns: Category -> column renames (see ``TABLES``).
        factors: Category -> stat per standings point (``load_sgp_factors``).
        playing_time: Playing-time column (``AB`` / ``INNs``).
        volume_per_team: Typical team playing time (see ``team_volume``).

    Returns:
        A copy of ``df`` with the SGP columns added (or unchanged, see above).
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


def replacement_positions(positions: str | None, player_type: str) -> list[str]:
    """Positions a player can be measured at for replacement: his own, plus UT for every hitter."""
    found = eligible_positions(positions, player_type)
    if player_type == "Batter" and "UT" not in found:
        found.append("UT")
    return found


def pitcher_roles(df: pd.DataFrame) -> pd.Series:
    """Each pitcher's role as ``["SP"]`` or ``["RP"]``, from how he is actually used.

    A pitcher who started at least half of his appearances is a starter, otherwise a
    reliever, whatever CBS lists him as eligible for. Pitchers with no appearances (or a
    table without ``GS``/``APP``) fall back to their CBS eligibility.
    """
    cbs = df["positions"].map(lambda value: eligible_positions(value, "Pitcher"))
    if not {"GS", "APP"} <= set(df.columns):
        return cbs
    roles = []
    for starts, appearances, fallback in zip(df["GS"].fillna(0), df["APP"].fillna(0), cbs):
        if appearances > 0:
            roles.append(["SP" if starts / appearances >= STARTER_SHARE else "RP"])
        else:
            roles.append(fallback)
    return pd.Series(roles, index=df.index)


def replacement_levels(
    df: pd.DataFrame, total_column: str, eligible: pd.Series, columns: list[str], candidates: pd.Series
) -> dict[str, dict[str, float]]:
    """Position -> ``{column: average value}`` of that position's replacement players.

    The replacement players at a position are the ``REPLACEMENT_POOL`` free agents
    eligible there with the highest ``total_column``. Every column's level is the
    average over those same players, so the category levels describe one realistic
    free agent and add up to the total's level. Only ``candidates`` (a boolean mask
    of eligible free agents) are considered.
    """
    pool = pd.DataFrame(
        {"position": eligible[candidates], "total": df.loc[candidates, total_column]}
    ).explode("position")
    pool = pool.dropna(subset=["position"])
    levels = {}
    for position, group in pool.groupby("position"):
        best = group.nlargest(REPLACEMENT_POOL, "total").index
        levels[position] = {column: df.loc[best, column].mean() for column in [total_column, *columns]}
    return levels


def active_pitcher_slots(lineups: pd.DataFrame | None = None) -> dict[str, float]:
    """Active slots per pitcher role on a typical team.

    Taken from the real active lineups when available. Otherwise ``PITCHING_ACTIVE_SLOTS``
    is split evenly between SP and RP, then kept within ``MAX_ACTIVE_SP`` and
    ``MAX_ACTIVE_RP`` (each role gets at least the slots the other role cannot take).
    """
    counts = lineup_slot_counts(lineups, "Pitcher")
    if {"SP", "RP"} <= counts.keys():
        return {"SP": counts["SP"], "RP": counts["RP"]}
    total = int(os.getenv("PITCHING_ACTIVE_SLOTS", DEFAULT_ACTIVE_SLOTS))
    max_sp = int(os.getenv("MAX_ACTIVE_SP", total))
    max_rp = int(os.getenv("MAX_ACTIVE_RP", total))
    starters = min(max(total // 2, total - max_rp), max_sp)
    return {"SP": starters, "RP": total - starters}


def slot_replacement_levels(
    df: pd.DataFrame, total_column: str, eligible: pd.Series, columns: list[str], slots: dict[str, float],
    candidates: pd.Series,
) -> dict[str, dict[str, float]]:
    """Position -> levels of the players around the last active slot at that position.

    With ``teams`` teams and ``slots`` active slots per team (not necessarily whole numbers
    when taken from real lineups), the last active player at a position is ranked
    ``teams * slots`` by ``total_column``. The level averages the
    ``REPLACEMENT_POOL`` players centered on that rank, over all candidates (owned or free).
    """
    teams = df["team_id"].nunique()
    levels = {}
    for position, count in slots.items():
        members = df[candidates & eligible.map(lambda found: position in found)]
        ranked = members.sort_values(total_column, ascending=False).index
        start = round(teams * count) - 1 - REPLACEMENT_POOL // 2
        window = ranked[max(start, 0):max(start, 0) + REPLACEMENT_POOL]
        if len(window) == REPLACEMENT_POOL:
            levels[position] = {column: df.loc[window, column].mean() for column in [total_column, *columns]}
    return levels


def compute_above_replacement(
    df: pd.DataFrame, player_type: str, total_column: str, category_columns: Sequence[str] = (),
    slots: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Add ``<column>_above_replacement`` and its ``_rank`` for the total and each category column.

    A player is measured at the eligible position with the lowest replacement total,
    i.e. where he adds the most over a free agent, and every category uses that same
    position's replacement players (see ``replacement_levels``). Every hitter also
    counts as UT-eligible, and pitchers are starters or relievers by usage
    (see ``pitcher_roles``). When ``slots`` (positions -> active slots per team) is given,
    a position's replacement is never lower than the players just outside the league's
    active slots there (see ``slot_replacement_levels``), whichever is higher: the waiver
    wire or the depth of the active pool. Only free agents with at least ``MIN_REPLACEMENT_VOLUME``
    of playing time can set a replacement level, so a few at-bats never define it.
    Players at a position with no replacement candidates, or without a score, get NaN.

    Args:
        df: Scored stats table with ``positions``, ``team_id`` (NULL for free agents) and
            ``total_column``.
        player_type: ``"Batter"`` or ``"Pitcher"``.
        total_column: Column of the total score (``z_total`` or ``sgp_total``).
        category_columns: Per-category score columns to measure the same way.
        slots: Position -> active slots per team, to apply the depth floor; None to skip it.

    Returns:
        A copy of ``df`` with ``<column>_above_replacement`` and ``..._rank`` columns.
    """
    df = df.copy()
    columns = list(category_columns)
    if player_type == "Pitcher":
        eligible = pitcher_roles(df)
    else:
        eligible = df["positions"].map(lambda value: replacement_positions(value, player_type))
    candidates = df["team_id"].isna() & df[total_column].notna()
    playing_time = PLAYING_TIME_COLUMN[player_type]
    if playing_time in df:
        candidates &= df[playing_time] >= MIN_REPLACEMENT_VOLUME[player_type]
    levels = replacement_levels(df, total_column, eligible, columns, candidates)
    if slots:
        pool = df[total_column].notna()
        if playing_time in df:
            pool &= df[playing_time] >= MIN_REPLACEMENT_VOLUME[player_type]
        for position, depth in slot_replacement_levels(df, total_column, eligible, columns, slots, pool).items():
            if position not in levels or depth[total_column] > levels[position][total_column]:
                levels[position] = depth

    def best_position(positions: list[str]) -> str | None:
        """The position with the lowest replacement total (ties by name), or None if none has a level."""
        options = [p for p in positions if p in levels]
        return min(options, key=lambda p: (levels[p][total_column], p)) if options else None

    chosen = eligible.map(best_position)
    for column in [total_column, *columns]:
        baseline = chosen.map(lambda position: levels[position][column] if isinstance(position, str) else float("nan"))
        name = f"{column}_{ABOVE_REPLACEMENT}"
        df[name] = df[column] - baseline
        df[f"{name}_rank"] = df[name].rank(ascending=False, method="min")
    return df


def score_frame(
    conn: db.Connection, df: pd.DataFrame, player_type: str, factors: dict[str, float]
) -> pd.DataFrame:
    """Add z-scores, SGP (when factors exist), above-replacement values and their ranks to ``df``.

    ``df`` needs the player type's stat columns, ``rank``, and (for SGP and replacement levels)
    ``team_id`` (NULL for free agents) and ``positions``. Returns ``df`` unchanged when no
    scoring categories are configured.

    Args:
        conn: Open database connection (for team volume and lineups).
        df: Stats table as stored in the ``batting`` or ``pitching`` table.
        player_type: ``"Batter"`` or ``"Pitcher"``.
        factors: Category -> stat per standings point; empty to skip SGP.

    Returns:
        The scored frame; stale ``z_``/``sgp_`` columns from a previous run are replaced.
    """
    categories = scoring_categories(player_type)
    if not categories:
        return df
    columns = TABLES[player_type][1]
    stale = [c for c in df.columns if c.startswith(("z_", "sgp_"))]
    playing_time = PLAYING_TIME_COLUMN[player_type]
    df = compute_z_scores(df.drop(columns=stale), categories, columns, playing_time)
    if factors and "team_id" in df:
        df = compute_sgp(df, categories, columns, factors, playing_time, team_volume(conn, df, player_type))
    if "team_id" in df and "positions" in df:
        lineups = latest_active_lineups(conn)
        # Hitters use the waiver wire alone; only pitchers get the depth floor.
        slots = active_pitcher_slots(lineups) if player_type == "Pitcher" else None
        for column in (z_column, sgp_column):
            if column(TOTAL) in df:
                df = compute_above_replacement(
                    df, player_type, column(TOTAL), [column(cat) for cat in categories if column(cat) in df],
                    slots,
                )
    return df


def save_z_scores() -> None:
    """Compute and store z-scores, SGP (when factors exist), above-replacement values and ranks in the batting and pitching tables."""
    with db.connect() as conn:
        factors = load_sgp_factors(conn)
        for player_type, (table, _columns) in TABLES.items():
            if not scoring_categories(player_type):
                continue
            df = conn.read_sql(f"SELECT * FROM {table}")  # noqa: S608 - fixed table names
            df = score_frame(conn, df, player_type, factors)
            if "team_id" in df:
                df["team_id"] = df["team_id"].astype("Int64")  # keep integers despite NULLs
            conn.write_frame(df, table)


def scoring_stat(row: db.Row, cat: str, columns: dict[str, str]) -> float | None:
    """A player's raw value in one scoring category, derived like ``_add_category``."""
    if cat == "W+QS":
        wins, quality_starts = row["W"], row["QS"]
        return None if wins is None or quality_starts is None else wins + quality_starts
    if cat == "Kd9":
        innings, strikeouts = row["INNs"], row["K"]
        return strikeouts * 9 / innings if innings and strikeouts is not None else None
    return row[columns.get(cat, cat)]


def _above_replacement(row: db.Row, column: Callable[[str], str], categories: list[str]) -> dict:
    """Total and per-category above-replacement values with ranks, as ``{key: (value, rank)}``.

    Args:
        row: Stored stats row.
        column: ``z_column`` or ``sgp_column``, choosing which score family to read.
        categories: Scoring categories to include besides the total.

    Returns:
        ``{key: (value, rank)}``, with None for a key lacking a value or rank.
    """
    result = {}
    for key in (TOTAL, *categories):
        name = f"{column(key)}_{ABOVE_REPLACEMENT}"
        value, rank = row[name], row[f"{name}_rank"]
        result[key] = (value, int(rank)) if value is not None and rank is not None else None
    return result


@dataclass
class ScoreContext:
    """What a stats table offers for one player type, so rows can be attached to players."""

    categories: list[str]
    columns: dict[str, str]
    has_sgp: bool
    has_z_repl: bool
    has_sgp_repl: bool


def _score_context(rows: list[db.Row], player_type: str) -> ScoreContext | None:
    """Describe the stored score columns, or None if the table can't be used for this player type."""
    categories = scoring_categories(player_type)
    if not categories:
        return None
    wanted = [TOTAL, *categories]
    stored = set(rows[0].keys()) if rows else set()
    if rows and any(z_column(c) not in stored for c in wanted):
        return None  # DB was imported with different scoring categories
    return ScoreContext(
        categories=categories,
        columns=TABLES[player_type][1],
        has_sgp=bool(rows) and all(sgp_column(c) in stored for c in wanted),
        has_z_repl=all(f"{z_column(c)}_{ABOVE_REPLACEMENT}" in stored for c in wanted),
        has_sgp_repl=all(f"{sgp_column(c)}_{ABOVE_REPLACEMENT}" in stored for c in wanted),
    )


def _int_or_none(value: float | None) -> int | None:
    """A rank as an int; projections can lack a category entirely, leaving no rank."""
    return None if value is None or value != value else int(value)


def attach_scores(player: models.Player, row: db.Row, context: ScoreContext, stats: bool = True) -> None:
    """Copy the stored scoring stats (unless ``stats`` is False), z-scores, SGP and above-replacement values onto ``player``."""
    categories = context.categories
    if stats:
        player.scoring_stats = {cat: scoring_stat(row, cat, context.columns) for cat in categories}
    rank = row["rank"]
    player.cbs_rank = int(rank) if rank is not None and rank < UNRANKED else None
    if row[z_column(TOTAL)] is None:
        return
    player.z_scores = {cat: row[z_column(cat)] for cat in categories}
    player.z_ranks = {cat: _int_or_none(row[f"{z_column(cat)}_rank"]) for cat in categories}
    player.z_total = row[z_column(TOTAL)]
    player.z_total_rank = int(row[f"{z_column(TOTAL)}_rank"])
    if context.has_sgp and row[sgp_column(TOTAL)] is not None:
        player.sgp = {cat: row[sgp_column(cat)] for cat in categories}
        player.sgp_ranks = {cat: _int_or_none(row[f"{sgp_column(cat)}_rank"]) for cat in categories}
        player.sgp_total = row[sgp_column(TOTAL)]
        player.sgp_total_rank = int(row[f"{sgp_column(TOTAL)}_rank"])
    if context.has_z_repl and row[f"{z_column(TOTAL)}_{ABOVE_REPLACEMENT}"] is not None:
        player.z_above_replacement = _above_replacement(row, z_column, categories)
    if context.has_sgp_repl and row[f"{sgp_column(TOTAL)}_{ABOVE_REPLACEMENT}"] is not None:
        player.sgp_above_replacement = _above_replacement(row, sgp_column, categories)


def _read_table(conn: db.Connection, table: str, where: str = "", params: tuple = ()) -> list[db.Row] | None:
    """Rows of a stats table (optionally filtered by a trusted ``where`` clause), or None if it is missing."""
    try:
        return conn.execute(f"SELECT * FROM {table} {where}", params).fetchall()  # noqa: S608 - fixed table names
    except db.SchemaError:
        return None


def _player_index(conn: db.Connection) -> PlayerIndex:
    """Name index over the players table; empty when the table has not been imported."""
    try:
        return PlayerIndex.load(conn)
    except RuntimeError:  # no players table: nothing can be matched
        return PlayerIndex([])


def evaluate_roster(roster: models.TeamRoster) -> None:
    """Attach the stored z-scores, SGP and ranks to each rostered player in place.

    Reads the columns written by ``save_z_scores``; players missing from the
    stats pool (or without playing time) are left without scores. SGP is
    attached only when the database has it. Players without a ``player_id`` (e.g. from an
    uploaded CBS export, which only has names) are resolved through the players table first.
    Does nothing if the database is unavailable.

    Args:
        roster: Roster whose players are updated in place.
    """
    conn = db.connect_or_none()
    if conn is None:
        return
    index: PlayerIndex | None = None
    with conn:
        for player_type, (table, _columns) in TABLES.items():
            players = [p for p in roster.players if p.player_type == player_type]
            if not players:
                continue
            rows = _read_table(conn, table)
            context = _score_context(rows, player_type) if rows is not None else None
            if context is None or (rows and "player_id" not in rows[0].keys()):
                continue  # stats imported before player ids existed; re-run scripts/refresh.py
            by_id: dict[str, list[db.Row]] = {}
            for row in rows:
                if row["player_id"] is not None:
                    by_id.setdefault(row["player_id"], []).append(row)
            for player in players:
                if player.player_id is None:  # e.g. an uploaded CBS roster, which only has names
                    if index is None:
                        index = _player_index(conn)
                    player.player_id = index.resolve(player.name, player.team, player_type)
                matches = by_id.get(player.player_id, [])
                same_team = [r for r in matches if r["team"] == player.team]
                matches = same_team or matches
                if matches:
                    attach_scores(player, matches[0], context)


def load_team_lineup(team_id: int) -> tuple[dict, models.TeamRoster] | None:
    """A fantasy team's most recent imported lineup as a scored roster, in lineup order.

    Returns ``(lineup, roster)`` where ``lineup`` is ``{"effective_date", "period_id", "captured_at"}``
    (the period is None for the offseason; ``captured_at`` is None for rows imported before it was
    recorded) and every player carries the lineup's slot and
    status plus any stored scores. Returns None if the team has no imported lineup.
    """
    conn = db.connect_or_none()
    if conn is None:
        return None
    with conn:
        try:
            rows = conn.execute(
                """
                SELECT * FROM lineups
                WHERE team_id = %s
                  AND CONCAT(effective_date, LPAD(IFNULL(period_id, 0), 6, '0')) = (
                      SELECT MAX(CONCAT(effective_date, LPAD(IFNULL(period_id, 0), 6, '0')))
                      FROM lineups WHERE team_id = %s)
                ORDER BY slot_order
                """,
                (team_id, team_id),
            ).fetchall()
        except db.SchemaError:
            return None
    if not rows:
        return None
    roster = models.TeamRoster(players=[
        models.Player(
            name=row["name"],
            player_id=row["player_id"],
            roster_slot=row["slot"],
            eligible_positions=[p.strip() for p in row["positions"].split(",") if p.strip()],
            team=row["mlb_team"],
            status=row["status"],
            player_type=row["player_type"],
        )
        for row in rows
    ])
    evaluate_roster(roster)
    captured_at = rows[0]["captured_at"] if "captured_at" in rows[0].keys() else None
    lineup = {"effective_date": rows[0]["effective_date"], "period_id": rows[0]["period_id"], "captured_at": captured_at}
    return lineup, roster


def load_team_roster(team_id: int) -> tuple[dict | None, dict[str, list[models.Player]]]:
    """A fantasy team's players in the order of its most recent imported lineup, with scores.

    Returns ``(lineup, players)`` (see ``load_team_lineup``) with the players split into
    ``{"Batter": [...], "Pitcher": [...]}``. Without a lineup for the team this falls back
    to ``load_players`` and ``lineup`` is None.
    """
    loaded = load_team_lineup(team_id)
    if loaded is None:
        return None, load_players(team_id)
    lineup, roster = loaded
    players = {player_type: [p for p in roster.players if p.player_type == player_type] for player_type in TABLES}
    return lineup, players


def load_players(team_id: int | None, limit: int | None = None) -> dict[str, list[models.Player]]:
    """Players from the stats database with their scores, best SGP vs replacement first.

    ``team_id`` selects a fantasy team's players, or the free agents when None.
    Free agents without playing time are left out. ``limit`` keeps only the best
    players of each type. Returns ``{"Batter": [...], "Pitcher": [...]}``.

    Args:
        team_id: Fantasy team id, or None for free agents.
        limit: Maximum players per type, or None for all.

    Returns:
        Players by type; empty lists when the database or stats tables are unavailable.
    """
    result: dict[str, list[models.Player]] = {player_type: [] for player_type in TABLES}
    conn = db.connect_or_none()
    if conn is None:
        return result
    where, params = ("WHERE team_id = %s", (team_id,)) if team_id is not None else ("WHERE team_id IS NULL", ())
    with conn:
        for player_type, (table, _columns) in TABLES.items():
            rows = _read_table(conn, table, where, params)
            context = _score_context(rows, player_type) if rows is not None else None
            if context is None:
                continue
            playing_time = PLAYING_TIME_COLUMN[player_type]
            for row in rows:
                if team_id is None and not (row[playing_time] or 0) > 0:
                    continue
                player = models.Player(
                    name=row["name"],
                    player_id=row["player_id"],
                    roster_slot="",
                    eligible_positions=[p.strip() for p in (row["positions"] or "").split(",") if p.strip()],
                    team=row["team"],
                    status="",
                    player_type=player_type,
                )
                attach_scores(player, row, context)
                result[player_type].append(player)
            result[player_type].sort(key=_value_rank)
            if limit is not None:
                result[player_type] = result[player_type][:limit]
    return result


# Sort key used when the caller does not choose one: CBS's own player rank.
FREE_AGENT_DEFAULT_SORT = "cbs"


def free_agent_sort(player_type: str, sort: str | None, direction: str | None) -> tuple[str, bool]:
    """Validate a free-agent sort key and direction, returning ``(key, descending)``.

    Keys are ``name``, ``bp``, ``cbs``, ``rank``, ``vr`` or ``stat:<category>``. Without a direction the
    natural one applies: best first, so highest for counting stats and SGP VR, lowest for ranks and ERA/WHIP.

    Args:
        player_type: ``"Batter"`` or ``"Pitcher"``; ``stat:`` keys must be one of its categories.
        sort: Sort key, or None/empty for ``FREE_AGENT_DEFAULT_SORT``.
        direction: ``"asc"``, ``"desc"`` or None/empty for the natural direction.

    Returns:
        ``(key, descending)``.

    Raises:
        ValueError: For an unknown key or direction. Keys end up in SQL, so only whitelisted
            values may pass.
    """
    key = sort or FREE_AGENT_DEFAULT_SORT
    if key.startswith("stat:"):
        category = key[5:]
        if category not in scoring_categories(player_type):
            raise ValueError(f"unknown sort {key!r}")
        natural = category not in LOWER_IS_BETTER
    elif key in ("name", "bp", "cbs", "rank", "vr"):
        natural = key == "vr"
    else:
        raise ValueError(f"unknown sort {key!r}")
    if direction not in (None, "", "asc", "desc"):
        raise ValueError(f"unknown direction {direction!r}")
    return key, natural if not direction else direction == "desc"


def _free_agent_sort_expression(player_type: str, key: str) -> str:
    """The SQL expression a free-agent sort key orders by."""
    if key == "name":
        return "name"
    if key == "cbs":
        return "`rank`"
    if key == "rank":
        return f"{sgp_column(TOTAL)}_{ABOVE_REPLACEMENT}_rank"
    if key == "vr":
        return f"{sgp_column(TOTAL)}_{ABOVE_REPLACEMENT}"
    category = key[5:]
    if category == "W+QS":
        return "(W + QS)"
    if category == "Kd9":
        return "(K * 9 / NULLIF(INNs, 0))"
    return f"`{TABLES[player_type][1].get(category, category)}`"


def load_free_agents(
    player_type: str, offset: int = 0, limit: int = 50, position: str | None = None,
    sort: str | None = None, direction: str | None = None,
) -> list[models.Player]:
    """One page of free agents of a type, best CBS rank first unless ``sort`` says otherwise.

    Sorting (see ``free_agent_sort``) and paging cover every free agent, not just those already shown, and
    happen in the database so only the requested rows are read; BP rankings live in another table, so that
    sort orders the matching ids in Python first. Free agents without playing time are left out; those with
    no value to sort by come last.

    Args:
        player_type: ``"Batter"`` or ``"Pitcher"``; anything else yields no players.
        offset: Number of free agents to skip.
        limit: Page size.
        position: Only players eligible at this whole position (``C`` does not match ``CF``).
        sort: Sort key (see ``free_agent_sort``).
        direction: ``"asc"``, ``"desc"`` or None for the natural direction.

    Returns:
        The page of players with scores attached; empty if the database or table is unavailable.

    Raises:
        ValueError: For an invalid ``sort`` or ``direction``.
    """
    if player_type not in TABLES:
        return []
    key, descending = free_agent_sort(player_type, sort, direction)
    table, _columns = TABLES[player_type]
    playing_time = PLAYING_TIME_COLUMN[player_type]
    params: tuple = ()
    position_filter = ""
    if position:
        # Positions are stored comma separated, so match whole entries only.
        position_filter = "AND FIND_IN_SET(%s, REPLACE(IFNULL(positions, ''), ' ', '')) > 0 "
        params = (position,)
    base = f"WHERE team_id IS NULL AND IFNULL(`{playing_time}`, 0) > 0 {position_filter}"
    conn = db.connect_or_none()
    if conn is None:
        return []
    with conn:
        if key == "bp":
            try:
                found = conn.execute(f"SELECT player_id, name FROM {table} {base}", params).fetchall()  # noqa: S608
            except db.SchemaError:
                return []
            ranks = bp_ranks()
            ranked = sorted((r for r in found if str(r["player_id"]) in ranks), key=lambda r: (ranks[str(r["player_id"])][0], r["name"]), reverse=descending)
            unranked = sorted((r for r in found if str(r["player_id"]) not in ranks), key=lambda r: r["name"])
            ids = [r["player_id"] for r in (ranked + unranked)[offset:offset + limit]]
            if not ids:
                return []
            marks = ", ".join(["%s"] * len(ids))
            rows = _read_table(conn, table, f"WHERE player_id IN ({marks})", tuple(ids))
            if rows is not None:
                by_id = {r["player_id"]: r for r in rows}
                rows = [by_id[i] for i in ids if i in by_id]
        else:
            expression = _free_agent_sort_expression(player_type, key)
            order = f"ORDER BY {expression} IS NULL, {expression} {'DESC' if descending else 'ASC'}, name LIMIT %s OFFSET %s"
            rows = _read_table(conn, table, base + order, (*params, limit, offset))
        context = _score_context(rows, player_type) if rows is not None else None
    if not rows or context is None:
        return []
    players = []
    for row in rows:
        player = models.Player(
            name=row["name"],
            player_id=row["player_id"],
            roster_slot="",
            eligible_positions=[p.strip() for p in (row["positions"] or "").split(",") if p.strip()],
            team=row["team"],
            status="",
            player_type=player_type,
        )
        attach_scores(player, row, context)
        players.append(player)
    return players


# Process-wide caches of slowly changing lookups, each ``(loaded_at, data)`` using
# ``time.monotonic``. They are rebuilt after ``CBS_IDS_TTL`` seconds so page renders do not
# query the database for every player. Note: an unreachable database is not cached, so the
# next call retries.
_CBS_IDS: tuple[float, dict[str, str]] | None = None
CBS_IDS_TTL = 300  # seconds


_BP_RANKS: tuple[float, dict[str, tuple[int, int | None]]] | None = None


def bp_ranks() -> dict[str, tuple[int, int | None]]:
    """Map player ids to Baseball Prospectus dynasty (rank, change) from its latest import, cached like ``cbs_ids``."""
    global _BP_RANKS
    now = time.monotonic()
    if _BP_RANKS is None or now - _BP_RANKS[0] > CBS_IDS_TTL:
        conn = db.connect_or_none()
        if conn is None:
            return {}
        with conn:
            try:
                rows = conn.execute(
                    """
                    SELECT player_id, `rank`, `change` FROM rankings_dynasty
                    WHERE source = 'bp' AND player_id IS NOT NULL
                      AND (season, snapshot) = (
                          SELECT season, snapshot FROM rankings_dynasty WHERE source = 'bp'
                          ORDER BY season DESC, snapshot DESC LIMIT 1)
                    """
                ).fetchall()
            except db.SchemaError:
                rows = []
        _BP_RANKS = (now, {str(pid): (int(rank), None if change is None else int(change)) for pid, rank, change in rows})
    return _BP_RANKS[1]


_PROSPECTS: tuple[float, dict[str, list[dict]]] | None = None
PROSPECT_MAX_RANK = 250  # a prospect needs a rank this good (or better) on some list to be shown
# Ranking source key -> display name.
PROSPECT_SOURCES = {"bp": "Baseball Prospectus", "fangraphs": "FanGraphs", "mlb": "MLB"}


def prospect_ranks() -> dict[str, list[dict]]:
    """Map player ids to their prospect list entries from the latest import of each source and list.

    Only players ranked ``PROSPECT_MAX_RANK`` or better on at least one list are included; each entry has ``list``, ``rank``,
    ``change``, ``org_rank``, ``fv``, ``level`` and ``eta``. Cached like ``cbs_ids``.
    """
    global _PROSPECTS
    now = time.monotonic()
    if _PROSPECTS is None or now - _PROSPECTS[0] > CBS_IDS_TTL:
        conn = db.connect_or_none()
        if conn is None:
            return {}
        with conn:
            try:
                rows = conn.execute(
                    "SELECT source, season, list_type, snapshot, player_id, `rank`, `change`, org_rank, fv, level, eta "
                    "FROM rankings_prospects WHERE player_id IS NOT NULL"
                ).fetchall()
            except db.SchemaError:
                rows = []
        latest: dict[tuple, tuple] = {}
        for row in rows:
            key = (row[0], row[2])
            latest[key] = max(latest.get(key, (row[1], row[3])), (row[1], row[3]))
        found: dict[str, list[dict]] = {}
        for source, season, list_type, snapshot, pid, rank, change, org_rank, fv, level, eta in rows:
            if (season, snapshot) != latest[(source, list_type)]:
                continue
            name = PROSPECT_SOURCES.get(source, source)
            label = name if list_type in ("batting", "pitching") else f"{name} {list_type.replace('_', ' ').title()}"
            found.setdefault(str(pid), []).append({
                "list": label, "rank": None if rank is None else int(rank),
                "change": None if change is None else int(change),
                "org_rank": None if org_rank is None else int(org_rank), "fv": fv, "level": level,
                "eta": None if eta is None else int(eta),
            })
        found = {pid: sorted(entries, key=lambda e: e["rank"] is None)
                 for pid, entries in found.items() if any(e["rank"] is not None and e["rank"] <= PROSPECT_MAX_RANK for e in entries)}
        _PROSPECTS = (now, found)
    return _PROSPECTS[1]


def cbs_ids() -> dict[str, str]:
    """Map player ids to CBS ids, reloaded from the database at most every ``CBS_IDS_TTL`` seconds."""
    global _CBS_IDS
    now = time.monotonic()
    if _CBS_IDS is None or now - _CBS_IDS[0] > CBS_IDS_TTL:
        conn = db.connect_or_none()
        if conn is None:
            return {}
        with conn:
            try:
                rows = conn.execute("SELECT player_id, cbs_id FROM players WHERE cbs_id IS NOT NULL AND cbs_id != ''").fetchall()
            except db.SchemaError:
                rows = []
        _CBS_IDS = (now, {str(pid): str(cbs) for pid, cbs in rows})
    return _CBS_IDS[1]


_EXTERNAL_IDS: tuple[float, dict[str, dict[str, str]]] | None = None


def external_ids() -> dict[str, dict[str, str]]:
    """Map player ids to their FanGraphs, MLB and BP ids (those that are known), cached like ``cbs_ids``."""
    global _EXTERNAL_IDS
    now = time.monotonic()
    if _EXTERNAL_IDS is None or now - _EXTERNAL_IDS[0] > CBS_IDS_TTL:
        conn = db.connect_or_none()
        if conn is None:
            return {}
        with conn:
            try:
                rows = conn.execute("SELECT player_id, fangraphs_id, mlb_id, bp_id FROM players").fetchall()
            except db.SchemaError:
                rows = []
        _EXTERNAL_IDS = (now, {
            str(pid): {key: str(value) for key, value in (("fangraphs", fg), ("mlb", mlb), ("bp", bp)) if value}
            for pid, fg, mlb, bp in rows
        })
    return _EXTERNAL_IDS[1]


def _value_rank(player: models.Player) -> tuple[int, int]:
    """Sort key: players with an SGP vs replacement rank first, best rank first."""
    total = player.sgp_above_replacement.get(TOTAL)
    return (0, total[1]) if total else (1, 0)

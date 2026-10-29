"""League z-score and SGP evaluation of rostered players against the top of the player pool."""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

try:
    from . import models
except ImportError:  # pragma: no cover - supports direct module imports
    import models

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "stats.db"
POOL_SIZE = 250
UNRANKED = 9999
DEFAULT_ACTIVE_SLOTS = 10
# Replacement level at a position is the average of this many best free agents there.
REPLACEMENT_POOL = 5
# Free agents need at least this much playing time (AB or innings) to count toward replacement level.
MIN_REPLACEMENT_VOLUME = {"Batter": 100, "Pitcher": 30}
PITCHER_POSITIONS = {"SP", "RP"}
# A pitcher who starts at least this share of his appearances is a starter, otherwise a reliever.
STARTER_SHARE = 0.5
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


def latest_active_lineups(conn: sqlite3.Connection) -> pd.DataFrame | None:
    """Players in each team's active slots in its most recent imported lineup, or None.

    Columns: ``team_id``, ``norm_name``, ``player_type`` and ``slot``. Needs the ``lineups``
    table from ``scripts/import_lineups.py``.
    """
    try:
        lineups = pd.read_sql_query(
            """
            SELECT l.team_id, l.norm_name, l.player_type, l.slot
            FROM lineups l
            JOIN (SELECT team_id, MAX(effective_date || printf('%06d', IFNULL(period_id, 0))) AS latest
                  FROM lineups GROUP BY team_id) m
              ON m.team_id = l.team_id
             AND l.effective_date || printf('%06d', IFNULL(l.period_id, 0)) = m.latest
            WHERE l.status = 'Active'
            """,
            conn,
        )
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
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

    Players are matched to the stats table by fantasy team and name. Returns None when
    no active player of that type can be matched.
    """
    playing_time = PLAYING_TIME_COLUMN[player_type]
    active = lineups[lineups["player_type"] == player_type][["team_id", "norm_name"]].astype({"team_id": "Int64"})
    if active.empty:
        return None
    stats = df[df["team_id"].notna()][["team_id", "norm_name", playing_time]].astype({"team_id": "Int64"})
    matched = active.merge(stats, on=["team_id", "norm_name"])
    if matched.empty:
        return None
    return float(matched.groupby("team_id")[playing_time].sum().mean())


def roster_coverage(conn: sqlite3.Connection, lineups: pd.DataFrame) -> float:
    """How much of a team's season innings its final active lineup accounts for, as a multiplier.

    The lineups are a snapshot, so players who threw innings earlier in the season for the
    team but are no longer active are missing. The real team innings from the standings,
    divided by the active pitchers' innings, measures that; it is at least 1.
    """
    try:
        innings = standings_innings(conn)
        pitching = pd.read_sql_query("SELECT team_id, norm_name, INNs FROM pitching", conn)
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return 1.0
    active = active_lineup_volume(pitching, lineups, "Pitcher")
    return max(innings / active, 1.0) if innings and active else 1.0


def team_volume(conn: sqlite3.Connection, df: pd.DataFrame, player_type: str) -> float:
    """Typical team playing time (AB or innings), from the best source available.

    Pitchers use the real team innings in the standings. Otherwise, and for batters, the
    active lineups are used: the playing time of the players in a team's active slots,
    scaled for batters by ``roster_coverage`` because a lineup is a snapshot. Without
    lineups, a team's most-used players stand in for its active slots.
    """
    playing_time = PLAYING_TIME_COLUMN[player_type]
    if player_type == "Pitcher":
        try:
            innings = standings_innings(conn)
        except sqlite3.OperationalError:
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
    df: pd.DataFrame, player_type: str, total_column: str, category_columns: list[str] = (),
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
        options = [p for p in positions if p in levels]
        return min(options, key=lambda p: (levels[p][total_column], p)) if options else None

    chosen = eligible.map(best_position)
    for column in [total_column, *columns]:
        baseline = chosen.map(lambda position: levels[position][column] if isinstance(position, str) else float("nan"))
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
                lineups = latest_active_lineups(conn)
                # Hitters use the waiver wire alone; only pitchers get the depth floor.
                slots = active_pitcher_slots(lineups) if player_type == "Pitcher" else None
                for column in (z_column, sgp_column):
                    if column(TOTAL) in df:
                        df = compute_above_replacement(
                            df, player_type, column(TOTAL), [column(cat) for cat in categories if column(cat) in df],
                            slots,
                        )
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


@dataclass
class _ScoreContext:
    """What a stats table offers for one player type, so rows can be attached to players."""

    categories: list[str]
    columns: dict[str, str]
    has_sgp: bool
    has_z_repl: bool
    has_sgp_repl: bool


def stats_db_path(db_path: str | Path | None = None) -> Path:
    return Path(db_path or os.getenv("STATS_DB") or DEFAULT_DB)


def _score_context(rows: list[sqlite3.Row], player_type: str) -> _ScoreContext | None:
    """Describe the stored score columns, or None if the table can't be used for this player type."""
    categories = scoring_categories(player_type)
    if not categories:
        return None
    wanted = [TOTAL, *categories]
    stored = set(rows[0].keys()) if rows else set()
    if rows and any(z_column(c) not in stored for c in wanted):
        return None  # DB was imported with different scoring categories
    return _ScoreContext(
        categories=categories,
        columns=TABLES[player_type][1],
        has_sgp=bool(rows) and all(sgp_column(c) in stored for c in wanted),
        has_z_repl=all(f"{z_column(c)}_{ABOVE_REPLACEMENT}" in stored for c in wanted),
        has_sgp_repl=all(f"{sgp_column(c)}_{ABOVE_REPLACEMENT}" in stored for c in wanted),
    )


def _attach_scores(player: models.Player, row: sqlite3.Row, context: _ScoreContext) -> None:
    """Copy the stored scoring stats, z-scores, SGP and above-replacement values onto ``player``."""
    categories = context.categories
    player.scoring_stats = {cat: scoring_stat(row, cat, context.columns) for cat in categories}
    rank = row["rank"]
    player.cbs_rank = int(rank) if rank is not None and rank < UNRANKED else None
    if row[z_column(TOTAL)] is None:
        return
    player.z_scores = {cat: row[z_column(cat)] for cat in categories}
    player.z_ranks = {cat: int(row[f"{z_column(cat)}_rank"]) for cat in categories}
    player.z_total = row[z_column(TOTAL)]
    player.z_total_rank = int(row[f"{z_column(TOTAL)}_rank"])
    if context.has_sgp and row[sgp_column(TOTAL)] is not None:
        player.sgp = {cat: row[sgp_column(cat)] for cat in categories}
        player.sgp_ranks = {cat: int(row[f"{sgp_column(cat)}_rank"]) for cat in categories}
        player.sgp_total = row[sgp_column(TOTAL)]
        player.sgp_total_rank = int(row[f"{sgp_column(TOTAL)}_rank"])
    if context.has_z_repl and row[f"{z_column(TOTAL)}_{ABOVE_REPLACEMENT}"] is not None:
        player.z_above_replacement = _above_replacement(row, z_column, categories)
    if context.has_sgp_repl and row[f"{sgp_column(TOTAL)}_{ABOVE_REPLACEMENT}"] is not None:
        player.sgp_above_replacement = _above_replacement(row, sgp_column, categories)


def _read_table(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> list[sqlite3.Row] | None:
    try:
        return conn.execute(f"SELECT * FROM {table} {where}", params).fetchall()  # noqa: S608 - fixed table names
    except sqlite3.OperationalError:
        return None


def evaluate_roster(roster: models.TeamRoster, db_path: str | Path | None = None) -> None:
    """Attach the stored z-scores, SGP and ranks to each rostered player in place.

    Reads the columns written by ``save_z_scores``; players missing from the
    stats pool (or without playing time) are left without scores. SGP is
    attached only when the database has it.
    """
    db_path = stats_db_path(db_path)
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for player_type, (table, _columns) in TABLES.items():
            players = [p for p in roster.players if p.player_type == player_type]
            if not players:
                continue
            rows = _read_table(conn, table)
            context = _score_context(rows, player_type) if rows is not None else None
            if context is None:
                continue
            by_name: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_name.setdefault(row["norm_name"], []).append(row)
            for player in players:
                matches = by_name.get(normalize_name(player.name), [])
                same_team = [r for r in matches if r["team"] == player.team]
                matches = same_team or matches
                if matches:
                    _attach_scores(player, matches[0], context)


def load_team_lineup(
    team_id: int, db_path: str | Path | None = None
) -> tuple[dict, models.TeamRoster] | None:
    """A fantasy team's most recent imported lineup as a scored roster, in lineup order.

    Returns ``(lineup, roster)`` where ``lineup`` is ``{"effective_date", "period_id"}`` (the
    period is None for the offseason) and every player carries the lineup's slot and
    status plus any stored scores. Returns None if the team has no imported lineup.
    """
    db_path = stats_db_path(db_path)
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT * FROM lineups
                WHERE team_id = ?
                  AND effective_date || printf('%06d', IFNULL(period_id, 0)) = (
                      SELECT MAX(effective_date || printf('%06d', IFNULL(period_id, 0)))
                      FROM lineups WHERE team_id = ?)
                ORDER BY slot_order
                """,
                (team_id, team_id),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
    if not rows:
        return None
    roster = models.TeamRoster(players=[
        models.Player(
            name=row["name"],
            roster_slot=row["slot"],
            eligible_positions=[p.strip() for p in row["positions"].split(",") if p.strip()],
            team=row["mlb_team"],
            status=row["status"],
            player_type=row["player_type"],
        )
        for row in rows
    ])
    evaluate_roster(roster, db_path)
    return {"effective_date": rows[0]["effective_date"], "period_id": rows[0]["period_id"]}, roster


def load_team_roster(
    team_id: int, db_path: str | Path | None = None
) -> tuple[dict | None, dict[str, list[models.Player]]]:
    """A fantasy team's players in the order of its most recent imported lineup, with scores.

    Returns ``(lineup, players)`` (see ``load_team_lineup``) with the players split into
    ``{"Batter": [...], "Pitcher": [...]}``. Without a lineup for the team this falls back
    to ``load_players`` and ``lineup`` is None.
    """
    loaded = load_team_lineup(team_id, db_path)
    if loaded is None:
        return None, load_players(team_id, db_path)
    lineup, roster = loaded
    players = {player_type: [p for p in roster.players if p.player_type == player_type] for player_type in TABLES}
    return lineup, players


def load_players(
    team_id: int | None, db_path: str | Path | None = None, limit: int | None = None
) -> dict[str, list[models.Player]]:
    """Players from the stats database with their scores, best SGP vs replacement first.

    ``team_id`` selects a fantasy team's players, or the free agents when None.
    Free agents without playing time are left out. ``limit`` keeps only the best
    players of each type. Returns ``{"Batter": [...], "Pitcher": [...]}``.
    """
    result: dict[str, list[models.Player]] = {player_type: [] for player_type in TABLES}
    db_path = stats_db_path(db_path)
    if not db_path.exists():
        return result
    where, params = ("WHERE team_id = ?", (team_id,)) if team_id is not None else ("WHERE team_id IS NULL", ())
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
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
                    roster_slot="",
                    eligible_positions=[p.strip() for p in (row["positions"] or "").split(",") if p.strip()],
                    team=row["team"],
                    status="",
                    player_type=player_type,
                )
                _attach_scores(player, row, context)
                result[player_type].append(player)
            result[player_type].sort(key=_value_rank)
            if limit is not None:
                result[player_type] = result[player_type][:limit]
    return result


def _value_rank(player: models.Player) -> tuple[int, int]:
    """Sort key: players with an SGP vs replacement rank first, best rank first."""
    total = player.sgp_above_replacement.get(TOTAL)
    return (0, total[1]) if total else (1, 0)

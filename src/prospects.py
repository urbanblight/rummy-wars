"""Prospect lists for the prospects page, from the ``rankings_prospects`` table."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from datetime import datetime, timezone

try:
    from . import db
    from .player_ids import normalize_name
except ImportError:  # pragma: no cover - supports direct module imports
    import db
    from player_ids import normalize_name

SORT_KEYS = ("name", "fangraphs", "bp", "mlb", "org", "age", "fv", "team")
DEFAULT_SORT = "rank"
BATTING_REPORT = [
    ("Hit", "hit"), ("Pitch Sel", "pitch_sel"), ("Bat Ctrl", "bat_ctrl"), ("Con Style", "con_style"),
    ("Game Pwr", "game_pwr"), ("Raw Pwr", "raw_pwr"), ("Spd", "spd"), ("Fld", "fld"),
    ("Hard Hit%", "hard_hit_pct"),
]
PITCHING_REPORT = [
    ("FB Type", "fb_type"), ("FB", "fb"), ("SL", "sl"), ("CB", "cb"), ("CH", "ch"), ("CMD", "cmd"),
    ("Sits", "sits"), ("Tops", "tops"), ("TJ Date", "tj_date"),
]
REPORTS = {"Batter": BATTING_REPORT, "Pitcher": PITCHING_REPORT}
COLUMNS = ("source", "list_type", "player_id", "`rank`", "`change`", "org_rank", "name", "team", "pos", "sec_pos",
           "age", "fv", "level", "eta", "hit", "pitch_sel", "bat_ctrl", "con_style", "game_pwr", "raw_pwr", "spd",
           "fld", "versa", "hard_hit_pct", "tj_date", "fb_type", "fb", "sl", "cb", "ch", "cmd", "sits", "tops")


@dataclass
class Prospect:
    name: str
    player_id: str | None = None
    team: str | None = None
    eligible_positions: list[str] = field(default_factory=list)
    fangraphs_rank: int | None = None
    bp_rank: int | None = None
    bp_change: int | None = None
    mlb_rank: int | None = None
    org_rank: int | None = None
    age: float | None = None
    fv: str | None = None
    level: str | None = None
    eta: str | None = None
    versa: str | None = None
    fantasy_team_id: int | None = None  # team whose latest imported lineup has the player
    fantasy_team: str | None = None
    report: list[list[str]] = field(default_factory=list)  # [label, value] scouting report pairs


def fv_number(fv: str | None) -> float | None:
    """A sortable number for a future value grade such as ``45`` or ``45+``."""
    if not fv:
        return None
    try:
        return float(fv.rstrip("+")) + (0.5 if fv.endswith("+") else 0)
    except ValueError:
        return None


def sort_value(prospect: Prospect, key: str):
    if key == "name":
        return normalize_name(prospect.name)
    if key == "team":
        # Blank sorts before every name, so players without a team lead an ascending sort.
        return normalize_name(prospect.fantasy_team) if prospect.fantasy_team else ""
    if key == "fv":
        return fv_number(prospect.fv)
    if key == "fangraphs":
        return prospect.fangraphs_rank
    if key == "bp":
        return prospect.bp_rank
    if key == "mlb":
        return prospect.mlb_rank
    if key == "org":
        return prospect.org_rank
    if key == "age":
        return prospect.age
    ranks = [r for r in (prospect.fangraphs_rank, prospect.bp_rank, prospect.mlb_rank) if r is not None]
    return min(ranks) if ranks else None


def sort_prospects(prospects: list[Prospect], key: str, descending: bool) -> list[Prospect]:
    """Sort by ``key``; players without a value come last (except teams, where blank sorts first)."""
    present = [p for p in prospects if sort_value(p, key) is not None]
    missing = [p for p in prospects if sort_value(p, key) is None]
    present.sort(key=lambda p: (sort_value(p, key), p.org_rank is None, p.org_rank or 0), reverse=descending)
    return present + missing


def _text(value) -> str | None:
    return None if value is None or str(value) == "" else str(value)


def build_prospects(rows: list, player_type: str) -> list[Prospect]:
    """Merge the latest FanGraphs, BP and MLB rows into one prospect per player."""
    report_columns = REPORTS[player_type]
    found: dict[str, Prospect] = {}
    for row in rows:
        key = row["player_id"] or normalize_name(row["name"])
        prospect = found.setdefault(key, Prospect(name=row["name"], player_id=row["player_id"]))
        prospect.team = prospect.team or row["team"]
        positions = [p for p in (row["pos"], *(row["sec_pos"] or "").split(",")) if p and p.strip() != "-"]
        for pos in positions:
            if pos.strip() not in prospect.eligible_positions:
                prospect.eligible_positions.append(pos.strip())
        if row["source"] == "fangraphs":
            prospect.fangraphs_rank = prospect.fangraphs_rank or row["rank"]
            prospect.org_rank = prospect.org_rank or row["org_rank"]
            prospect.age = prospect.age if prospect.age is not None else row["age"]
            prospect.fv = prospect.fv or row["fv"]
            prospect.versa = prospect.versa or _text(row["versa"])
        elif row["source"] == "mlb":
            prospect.mlb_rank = prospect.mlb_rank or row["rank"]
        elif row["source"] == "bp":
            if prospect.bp_rank is None:
                prospect.bp_rank, prospect.bp_change = row["rank"], row["change"]
            prospect.level = prospect.level or _text(row["level"])
            prospect.eta = prospect.eta or _text(row["eta"])
        have = {label for label, _ in prospect.report}
        for label, column in report_columns:
            value = _text(row[column])
            if value is not None and label not in have:
                prospect.report.append([label, value])
    return list(found.values())


def fantasy_teams(conn: db.Connection) -> dict[str, tuple[int, str]] | None:
    """Player id -> (team id, team name) from each team's most recent imported lineup, or None if none imported."""
    try:
        rows = conn.execute(
            """
            SELECT l.player_id, t.id, t.name
            FROM lineups l
            JOIN (SELECT team_id, MAX(CONCAT(effective_date, LPAD(IFNULL(period_id, 0), 6, '0'))) AS latest
                  FROM lineups GROUP BY team_id) m
              ON m.team_id = l.team_id
             AND CONCAT(l.effective_date, LPAD(IFNULL(l.period_id, 0), 6, '0')) = m.latest
            JOIN teams t ON t.id = l.team_id
            WHERE l.player_id IS NOT NULL
            """
        ).fetchall()
    except db.SchemaError:
        return None
    return {str(player_id): (team_id, name) for player_id, team_id, name in rows} or None


SOURCE_LABELS = {"fangraphs": "FanGraphs", "bp": "Baseball Prospectus", "mlb": "MLB"}


def last_updated() -> list[tuple[str, datetime]]:
    """(source label, time of its latest import) for each source with imported prospects."""
    conn = db.connect_or_none()
    if conn is None:
        return []
    with conn:
        try:
            rows = conn.execute(
                "SELECT source, MAX(snapshot) FROM rankings_prospects GROUP BY source"
            ).fetchall()
        except db.SchemaError:
            return []
    updated = []
    for source, snapshot in sorted(rows, key=lambda row: row[0] != "fangraphs"):
        try:
            when = datetime.strptime(snapshot, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        updated.append((SOURCE_LABELS.get(source, source), when))
    return updated


def load_prospects(player_type: str, sort: str = DEFAULT_SORT, descending: bool = False) -> list[Prospect]:
    """The latest FanGraphs, BP and MLB prospects of one type (``Batter`` or ``Pitcher``)."""
    conn = db.connect_or_none()
    if conn is None:
        return []
    with conn:
        try:
            lists = conn.execute(
                "SELECT source, list_type, season, snapshot FROM rankings_prospects "
                "WHERE player_type = %s AND source IN ('fangraphs', 'bp', 'mlb') GROUP BY source, list_type, season, snapshot",
                (player_type,),
            ).fetchall()
            latest: dict[str, tuple] = {}
            for source, list_type, season, snapshot in lists:
                latest[source] = max(latest.get(source, (season, snapshot, list_type)), (season, snapshot, list_type))
            rows = []
            for source, (season, snapshot, list_type) in sorted(latest.items(), reverse=True):
                rows += conn.execute(
                    f"SELECT {', '.join(COLUMNS)} FROM rankings_prospects "  # noqa: S608
                    "WHERE source = %s AND season = %s AND list_type = %s AND snapshot = %s AND player_type = %s",
                    (source, season, list_type, snapshot, player_type),
                ).fetchall()
        except db.SchemaError:
            return []
        owners = fantasy_teams(conn)
    found = build_prospects(rows, player_type)
    for prospect in found:
        if owners and prospect.player_id in owners:
            prospect.fantasy_team_id, prospect.fantasy_team = owners[prospect.player_id]
    return sort_prospects(found, sort, descending)


_PROFILES: tuple[float, dict[str, Prospect]] | None = None
PROFILES_TTL = 300  # seconds


def profiles() -> dict[str, Prospect]:
    """The latest prospect record (age, scouting report) by player id, cached for ``PROFILES_TTL`` seconds."""
    global _PROFILES
    now = time.monotonic()
    if _PROFILES is None or now - _PROFILES[0] > PROFILES_TTL:
        found = {p.player_id: p for kind in ("Batter", "Pitcher") for p in load_prospects(kind) if p.player_id}
        _PROFILES = (now, found)
    return _PROFILES[1]

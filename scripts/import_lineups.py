#!/usr/bin/env python3
"""Import CBS roster-overview (lineup) exports into the MySQL database.

Usage:
    python scripts/import_lineups.py [PATH ...]

Each PATH is a lineup file or a folder of them (default: ``data/lineups``). Files are
named after the lineup they hold:

* ``roster-overview-<team-id>-<date>.csv`` for the offseason lineup, or
* ``roster-overview-<team-id>-<period-id>-<date>.csv`` for an in-season lineup,

where ``<date>`` is the effective date as YYYYMMDD. Run ``import_teams.py`` and
``import_players.py`` first, since each lineup belongs to a team and each player is
stored by canonical ``player_id`` (NULL, with a warning, for a name the ID map does not
know). Every player in a file becomes one row of the ``lineups``
table, keyed by team, period (NULL for the offseason), effective date and ``slot_order``
(the player's order in the file). Importing a lineup again replaces its rows. Each row also
records ``captured_at``, the UTC time of the import, which is distinct from the effective
date taken from the file name. Files in a
folder whose names do not match are skipped with a warning.

The per-player scoring stats in the export are not stored: they come from the stats
tables (see ``import_cbs_stats.py``). The schedule column is not stored either.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import db  # noqa: E402
from import_cbs_stats import PLAYER_PATTERN  # noqa: E402
from player_ids import PlayerIndex  # noqa: E402

DEFAULT_DIR = REPO_ROOT / "data" / "lineups"
FILE_PATTERN = re.compile(r"^roster-overview-(\d+)(?:-(\d+))?-(\d{8})\.csv$")
TEAM_FILE_PATTERN = re.compile(r"^roster-overview-(?:(\d+)-)?(\d{8})\.csv$")

# Section titles in the export. Batters/Pitchers hold the active lineup; the others
# follow and belong to the most recent of those two.
GROUPS = {"Batters": "Batter", "Pitchers": "Pitcher"}
STATUSES = {"Batters": "Active", "Pitchers": "Active", "Reserves": "Reserves", "Injured": "Injured", "Minors": "Minors"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineups (
    team_id INT NOT NULL,
    period_id INT,
    effective_date VARCHAR(10) NOT NULL,
    slot_order INT NOT NULL,
    player_type VARCHAR(16) NOT NULL CHECK (player_type IN ('Batter', 'Pitcher')),
    status VARCHAR(16) NOT NULL CHECK (status IN ('Active', 'Reserves', 'Injured', 'Minors')),
    slot VARCHAR(16) NOT NULL,
    player_id VARCHAR(32),
    name VARCHAR(255) NOT NULL,
    positions VARCHAR(64) NOT NULL,
    mlb_team VARCHAR(16) NOT NULL,
    proj_rank INT,
    actual_rank INT,
    rostered_pct DOUBLE,
    started_pct DOUBLE,
    captured_at VARCHAR(32),
    UNIQUE KEY uq_lineups_slot (team_id, (IFNULL(period_id, -1)), effective_date, slot_order),
    KEY idx_lineups_player (player_id),
    FOREIGN KEY (team_id) REFERENCES teams (id),
    FOREIGN KEY (player_id) REFERENCES players (player_id)
);
"""

COLUMNS = (
    "team_id", "period_id", "effective_date", "slot_order", "player_type", "status", "slot",
    "player_id", "name", "positions", "mlb_team", "proj_rank", "actual_rank", "rostered_pct", "started_pct",
)


@dataclass(frozen=True)
class LineupFile:
    team_id: int
    period_id: int | None
    effective_date: str  # ISO YYYY-MM-DD


def parse_filename(name: str) -> LineupFile:
    """Read the team, optional period and effective date from a lineup file name."""
    match = FILE_PATTERN.match(name)
    if not match:
        raise ValueError(
            f"{name}: expected roster-overview-<team-id>-<date>.csv or "
            "roster-overview-<team-id>-<period-id>-<date>.csv"
        )
    team_id, period_id, stamp = match.groups()
    try:
        effective = datetime.strptime(stamp, "%Y%m%d").date()
    except ValueError as error:
        raise ValueError(f"{name}: {stamp} is not a YYYYMMDD date") from error
    return LineupFile(int(team_id), int(period_id) if period_id else None, effective.isoformat())


def parse_team_filename(name: str, team_id: int) -> LineupFile:
    """Like ``parse_filename`` for a name without the team id (the team is known from context).

    Accepts ``roster-overview-<date>.csv`` and ``roster-overview-<period-id>-<date>.csv``.
    """
    match = TEAM_FILE_PATTERN.match(name)
    if not match:
        raise ValueError(
            f"{name}: expected roster-overview-<date>.csv or roster-overview-<period-id>-<date>.csv"
        )
    period_id, stamp = match.groups()
    try:
        effective = datetime.strptime(stamp, "%Y%m%d").date()
    except ValueError as error:
        raise ValueError(f"{name}: {stamp} is not a YYYYMMDD date") from error
    return LineupFile(team_id, int(period_id) if period_id else None, effective.isoformat())


def _number(value: str, kind=float):
    value = value.strip().rstrip("%").replace(",", "")
    try:
        return kind(value)
    except ValueError:
        return None  # "N/A" and blanks


def parse_lineup(text: str) -> list[dict]:
    """Players in file order, as dicts with the lineup column values (no team or date)."""
    players: list[dict] = []
    player_type = None
    status = None
    for fields in csv.reader(io.StringIO(text)):
        if not fields or not any(field.strip() for field in fields):
            continue
        first = fields[0].strip()
        if not any(field.strip() for field in fields[1:]):
            if first in STATUSES:
                status = STATUSES[first]
                player_type = GROUPS.get(first, player_type)
            continue  # also skips the "Active: 20 Reserve: 7 ..." footer
        if len(fields) < 8 or fields[1].strip() in ("Pos", "") or not fields[2].strip():
            continue  # column headers and the stat totals row
        if status is None or player_type is None:
            raise ValueError(f"player row before any section heading: {fields[2].strip()!r}")
        cell = fields[2].strip()
        parsed = PLAYER_PATTERN.match(cell)
        if not parsed:
            raise ValueError(f"cannot read player {cell!r} (expected 'Name POS,POS | TEAM')")
        name, positions, mlb_team = (part.strip() for part in parsed.groups())
        players.append({
            "slot_order": len(players) + 1,
            "player_type": player_type,
            "status": status,
            "slot": fields[1].strip(),
            "name": name,
            "positions": positions,
            "mlb_team": mlb_team,
            "proj_rank": _number(fields[4], int),
            "actual_rank": _number(fields[5], int),
            "rostered_pct": _number(fields[6]),
            "started_pct": _number(fields[7]),
        })
    if not players:
        raise ValueError("no players found")
    return players


def ensure_schema(conn: db.Connection) -> None:
    """Create the lineups table, rebuilding one that predates ``player_id`` (it is re-importable)."""
    columns = db.column_names(conn, "lineups")
    if columns and "player_id" not in columns:
        conn.execute("DROP TABLE lineups")
    elif columns and "captured_at" not in columns:
        conn.execute("ALTER TABLE lineups ADD COLUMN captured_at VARCHAR(32)")
    conn.executescript(SCHEMA)


def store_lineup(
    conn: db.Connection, lineup: LineupFile, players: list[dict], index: PlayerIndex, source: str
) -> None:
    """Replace one lineup's rows with ``players``; importing the same lineup again is a no-op in effect."""
    if not conn.execute("SELECT 1 FROM teams WHERE id = %s", (lineup.team_id,)).fetchone():
        raise ValueError(f"{source}: no team with id {lineup.team_id}; run scripts/import_teams.py first")
    for player in players:
        player["player_id"] = index.resolve(player["name"], player["mlb_team"], player["player_type"])
        if player["player_id"] is None:
            print(f"Warning: {source}: no player id for {player['name']} ({player['mlb_team']})", file=sys.stderr)
    conn.execute(
        "DELETE FROM lineups WHERE team_id = %s AND period_id <=> %s AND effective_date = %s",
        (lineup.team_id, lineup.period_id, lineup.effective_date),
    )
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        f"INSERT INTO lineups ({', '.join(COLUMNS)}, captured_at) VALUES ({', '.join(['%s'] * (len(COLUMNS) + 1))})",
        [
            (lineup.team_id, lineup.period_id, lineup.effective_date, *(p[c] for c in COLUMNS[3:]), captured_at)
            for p in players
        ],
    )


def import_lineup(path: Path, conn: db.Connection, index: PlayerIndex | None = None) -> tuple[LineupFile, int]:
    """Replace one lineup's rows with the contents of ``path``; returns (lineup, players)."""
    index = index or PlayerIndex.load(conn)
    lineup = parse_filename(path.name)
    players = parse_lineup(path.read_text(encoding="utf-8-sig"))
    store_lineup(conn, lineup, players, index, path.name)
    return lineup, len(players)


def find_files(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Expand folders into their CSV files; returns (lineup files, skipped files)."""
    files, skipped = [], []
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.glob("*.csv")):
                (files if FILE_PATTERN.match(candidate.name) else skipped).append(candidate)
        else:
            files.append(path)
    return files, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help=f"lineup files or folders (default: {DEFAULT_DIR})")
    args = parser.parse_args(argv)

    files, skipped = find_files(args.paths or [DEFAULT_DIR])
    for path in skipped:
        print(f"Skipping {path.name}: not a roster-overview file name", file=sys.stderr)
    if not files:
        print("No lineup files found", file=sys.stderr)
        return 1

    imported = 0
    try:
        with db.connect() as conn:
            ensure_schema(conn)
            index = PlayerIndex.load(conn)
            for path in files:
                lineup, count = import_lineup(path, conn, index)
                period = f"period {lineup.period_id}" if lineup.period_id is not None else "offseason"
                print(f"Team {lineup.team_id} ({period}, {lineup.effective_date}): {count} players")
                imported += 1
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, db.Error, db.NotConfigured) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Imported {imported} lineups")
    return 0


if __name__ == "__main__":
    sys.exit(main())

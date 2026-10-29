#!/usr/bin/env python3
"""Import the SFBB player ID map into a sqlite database.

Usage:
    python scripts/import_players.py [--source URL_OR_CSV] [--db data/stats.db]

Reads the Smart Fantasy Baseball player ID map (default: the live CSV download; pass a
local file to work offline) and upserts the ``players`` table, keyed by the map's
canonical ``IDPLAYER`` (``player_id``) with the FanGraphs, CBS, MLB and Baseball
Prospectus ids alongside. Rerunning updates existing players in place, so it is safe to
repeat; players that drop out of the map are kept, because lineups, batting and pitching
rows may still refer to them. Run this before ``import_lineups.py`` and
``import_cbs_stats.py``, which look up each player's ``player_id`` by name and MLB team.
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = "https://www.smartfantasybaseball.com/PLAYERIDMAPCSV"

# Map column (first one present wins) -> players column.
COLUMNS = {
    "IDPLAYER": ("player_id",),
    "PLAYERNAME": ("name",),
    "CBSNAME": ("cbs_name",),
    "TEAM": ("team",),
    "POS": ("pos",),
    "ACTIVE": ("active",),
    "IDFANGRAPHS": ("fangraphs_id",),
    "FANGRAPHSID": ("fangraphs_id",),
    "CBSID": ("cbs_id",),
    "BPID": ("bp_id",),
    "MLBID": ("mlb_id",),
}
REQUIRED = ("IDPLAYER", "PLAYERNAME")

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cbs_name TEXT,
    team TEXT,
    pos TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    fangraphs_id TEXT,
    cbs_id TEXT,
    mlb_id TEXT,
    bp_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_players_name ON players (name);
CREATE INDEX IF NOT EXISTS idx_players_cbs_name ON players (cbs_name);
"""

FIELDS = ("player_id", "name", "cbs_name", "team", "pos", "active", "fangraphs_id", "cbs_id", "mlb_id", "bp_id")


def load_players(source: str | Path) -> pd.DataFrame:
    """Read the ID map and keep the mapping columns, one row per canonical id."""
    if str(source).startswith(("http://", "https://")):
        response = requests.get(source, headers={"User-Agent": "rummy-wars"}, timeout=60)
        response.raise_for_status()
        source = io.StringIO(response.text)
    df = pd.read_csv(source, dtype=str, keep_default_na=False, na_values=["", "N/A"])
    missing = [column for column in REQUIRED if column not in df.columns]
    if missing:
        raise ValueError(f"player ID map: missing column(s) {', '.join(missing)}")
    out = pd.DataFrame(index=df.index)
    for column, (target,) in COLUMNS.items():
        if column in df.columns and target not in out:
            out[target] = df[column].str.strip()
    for field in FIELDS:
        if field not in out:
            out[field] = None
    out = out[out["player_id"].notna() & out["name"].notna()].drop_duplicates("player_id", keep="first")
    out["active"] = (out["active"].str.upper() == "Y").astype(int)
    return out[list(FIELDS)].astype(object).where(out[list(FIELDS)].notna(), None).reset_index(drop=True)


def import_players(source: str | Path, db_path: Path) -> int:
    players = load_players(source)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    updates = ", ".join(f"{field} = excluded.{field}" for field in FIELDS[1:])
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            f"INSERT INTO players ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))}) "  # noqa: S608
            f"ON CONFLICT(player_id) DO UPDATE SET {updates}",
            players.itertuples(index=False, name=None),
        )
    return len(players)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="player ID map URL or CSV file")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "stats.db")
    args = parser.parse_args(argv)
    try:
        count = import_players(args.source, args.db)
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    except (ValueError, OSError, requests.RequestException, pd.errors.ParserError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Imported {count} players into {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

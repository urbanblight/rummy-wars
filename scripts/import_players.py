#!/usr/bin/env python3
"""Import the SFBB player ID map into the MySQL database.

Usage:
    python scripts/import_players.py [--source URL_OR_CSV]

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
import sys
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import db  # noqa: E402

# Live download of the Smart Fantasy Baseball ID map.
DEFAULT_SOURCE = "https://www.smartfantasybaseball.com/PLAYERIDMAPCSV"

# Map column -> players column. FanGraphs id appears under two header spellings; the
# first one present in the file wins.
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
# Without these two columns a row cannot be keyed or named, so the file is rejected.
REQUIRED = ("IDPLAYER", "PLAYERNAME")

# DDL for ``players``; ``name`` and ``cbs_name`` are indexed for name-based lookups.
SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    player_id VARCHAR(32) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    cbs_name VARCHAR(255),
    team VARCHAR(16),
    pos VARCHAR(32),
    active INT NOT NULL DEFAULT 0,
    fangraphs_id VARCHAR(32),
    cbs_id VARCHAR(32),
    mlb_id VARCHAR(32),
    bp_id VARCHAR(32),
    INDEX idx_players_name (name),
    INDEX idx_players_cbs_name (cbs_name)
);
"""

# Final column order, used for both the DataFrame and the INSERT.
FIELDS = ("player_id", "name", "cbs_name", "team", "pos", "active", "fangraphs_id", "cbs_id", "mlb_id", "bp_id")


def load_players(source: str | Path) -> pd.DataFrame:
    """Read the ID map and keep the mapping columns, one row per canonical id.

    Args:
        source: ``http(s)`` URL or path of a local CSV.

    Returns:
        A DataFrame with exactly the ``FIELDS`` columns; absent values are None and
        ``active`` is 1/0. Rows without an id or name are dropped, and the first row
        wins when an id repeats.

    Raises:
        ValueError: If a required column is missing.
        requests.RequestException: If the download fails.
    """
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


def import_players(source: str | Path) -> int:
    """Upsert the ID map into ``players``.

    Args:
        source: URL or path accepted by ``load_players``.

    Returns:
        Number of players read from the map.
    """
    players = load_players(source)
    updates = ", ".join(f"{field} = new.{field}" for field in FIELDS[1:])
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            f"INSERT INTO players ({', '.join(FIELDS)}) VALUES ({', '.join(['%s'] * len(FIELDS))}) AS new "  # noqa: S608
            f"ON DUPLICATE KEY UPDATE {updates}",
            players.itertuples(index=False, name=None),
        )
    return len(players)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        Exit status: 0 on success, 1 on any error.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="player ID map URL or CSV file")
    args = parser.parse_args(argv)
    try:
        count = import_players(args.source)
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    except (ValueError, OSError, requests.RequestException, pd.errors.ParserError, db.Error, db.NotConfigured) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Imported {count} players")
    return 0


if __name__ == "__main__":
    sys.exit(main())

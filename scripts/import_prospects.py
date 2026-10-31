#!/usr/bin/env python3
"""Import prospect ranking CSVs into the ``rankings_prospects`` table.

Usage:
    python scripts/import_prospects.py [PATH ...]

Each PATH is a prospect export or a folder of them (default: ``data/prospects``). Files are
named ``<source>-<year>-<type>.csv``: ``fangraphs-2026-batting.csv`` and
``fangraphs-2026-pitching.csv`` (FanGraphs scouting grades) or ``bp-2026-midseason.csv``
(a Baseball Prospectus list of batters and pitchers). The source, season and list type come
from the name. Columns are recognised by header:

* ``Rank`` (BP) or ``Top 100`` (FanGraphs) is stored as ``rank``; FanGraphs players outside
  the top 100 have a NULL rank, with their ``Org Rk`` and ``FV`` still stored.
* ``Change``, ``Level``, ``ETA``, ``Pos``/``POS.``, ``SEC. POS.``, ``Team``/``Org`` and, for
  FanGraphs, ``Age``, the hitting grades and the pitch grades are stored as well.
* BP's ``Age`` and its trailing column named for the previous list (``October``) are ignored.

FanGraphs batting and pitching files list every prospect, so a row is kept only when it has
data for that file's player type. In a ``midseason``-style file a pitching position (LHP/RHP)
marks a pitcher. Run ``import_players.py`` first: each ``player_id`` is matched from the name
(and team) against the ``players`` table, NULL with a warning if unknown.

Every import is stored under a new ``snapshot`` (the UTC time it was inserted), so earlier
lists are kept; the latest snapshot of a source, season and type is the current ranking.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import db  # noqa: E402
from player_ids import PlayerIndex  # noqa: E402

load_dotenv(REPO_ROOT / ".env", override=False)
DEFAULT_DIR = REPO_ROOT / "data" / "prospects"
FILE_PATTERN = re.compile(r"^(.+)-(\d{4})-([A-Za-z0-9_]+)\.csv$")
PITCHER_POSITIONS = {"P", "SP", "RP", "SIRP", "MIRP", "LHP", "RHP"}

# Export header (lower case) -> table column. Anything not listed (BP's Age and its
# previous-list column) is ignored.
SHARED_COLUMNS = {
    "rank": "rank", "top 100": "rank", "change": "change", "player": "name", "name": "name",
    "team": "team", "org": "team", "pos": "pos", "pos.": "pos", "sec. pos.": "sec_pos",
    "org rk": "org_rank", "eta": "eta", "level": "level", "fv": "fv", "playerid": "fangraphs_id",
}
BATTING_COLUMNS = {
    "age": "age", "hit": "hit", "pitch sel": "pitch_sel", "bat ctrl": "bat_ctrl",
    "con style": "con_style", "game pwr": "game_pwr", "raw pwr": "raw_pwr", "spd": "spd",
    "fld": "fld", "versa": "versa", "hard hit%": "hard_hit_pct",
}
PITCHING_COLUMNS = {
    "age": "age", "tj date": "tj_date", "fb type": "fb_type", "fb": "fb", "sl": "sl", "cb": "cb",
    "ch": "ch", "cmd": "cmd", "sits": "sits", "tops": "tops",
}
GRADE_COLUMNS = {"batting": BATTING_COLUMNS, "pitching": PITCHING_COLUMNS}
INTEGER_COLUMNS = ["rank", "change", "org_rank", "eta"]
# Columns that only exist for one player type; a row needs one of them in a batting/pitching file.
SPECIFIC = {kind: [c for c in columns.values() if c != "age"] for kind, columns in GRADE_COLUMNS.items()}
TABLE_COLUMNS = [
    "rank", "change", "org_rank", "player_id", "fangraphs_id", "name", "team", "pos", "sec_pos",
    "player_type", "level", "eta", "age", "fv",
    *SPECIFIC["batting"], *SPECIFIC["pitching"],
]
SCHEMA = """
CREATE TABLE IF NOT EXISTS rankings_prospects (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    season INT NOT NULL,
    list_type VARCHAR(32) NOT NULL,
    snapshot VARCHAR(32) NOT NULL,
    `rank` INT,
    `change` INT,
    org_rank INT,
    player_id VARCHAR(32),
    fangraphs_id VARCHAR(32),
    name VARCHAR(255) NOT NULL,
    team VARCHAR(32),
    pos VARCHAR(16),
    sec_pos VARCHAR(32),
    player_type VARCHAR(16) NOT NULL,
    level VARCHAR(32),
    eta INT,
    age DOUBLE,
    fv VARCHAR(8),
    hit VARCHAR(16),
    pitch_sel VARCHAR(16),
    bat_ctrl VARCHAR(16),
    con_style VARCHAR(32),
    game_pwr VARCHAR(16),
    raw_pwr VARCHAR(16),
    spd VARCHAR(16),
    fld VARCHAR(16),
    versa VARCHAR(32),
    hard_hit_pct DOUBLE,
    tj_date DATE,
    fb_type VARCHAR(32),
    fb VARCHAR(16),
    sl VARCHAR(16),
    cb VARCHAR(16),
    ch VARCHAR(16),
    cmd VARCHAR(16),
    sits VARCHAR(16),
    tops VARCHAR(16),
    KEY idx_rankings_prospects_list (source, season, list_type, snapshot),
    KEY idx_rankings_prospects_player (player_id),
    FOREIGN KEY (player_id) REFERENCES players (player_id)
);
"""


def parse_filename(name: str) -> tuple[str, int, str]:
    """Read (source, season, list type) from ``<source>-<year>-<type>.csv``."""
    match = FILE_PATTERN.match(name)
    if not match:
        raise ValueError(f"{name}: expected <source>-<year>-<type>.csv")
    source, season, list_type = match.groups()
    return source.lower(), int(season), list_type.lower()


def find_files(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Expand folders into their prospect files; returns (prospect files, skipped files)."""
    files, skipped = [], []
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.glob("*.csv")):
                (files if FILE_PATTERN.match(candidate.name) else skipped).append(candidate)
        else:
            files.append(path)
    return files, skipped


def _text(series: pd.Series) -> pd.Series:
    cleaned = series.astype("string").str.strip()
    return cleaned.mask(cleaned.isin(["", "-"]))


def load_prospects(path: Path, index: PlayerIndex) -> pd.DataFrame:
    _, _, list_type = parse_filename(path.name)
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    df.columns = [str(column).strip() for column in df.columns]
    mapping = {**SHARED_COLUMNS, **(GRADE_COLUMNS.get(list_type) or {})}
    renamed: dict[str, str] = {}
    for column in df.columns:
        target = mapping.get(column.lower())
        if target and target not in renamed.values():
            renamed[column] = target
    df = df[list(renamed)].rename(columns=renamed)
    if "name" not in df.columns:
        raise ValueError(f"{path.name}: no Player/Name column")
    out = pd.DataFrame({column: _text(df[column]) if column in df else pd.NA for column in TABLE_COLUMNS})
    out = out[out["name"].notna()].copy()

    if list_type in SPECIFIC:
        out = out[out[SPECIFIC[list_type]].notna().any(axis=1)].copy()
        out["player_type"] = "Pitcher" if list_type == "pitching" else "Batter"
    else:
        out["player_type"] = ["Pitcher" if str(pos).upper() in PITCHER_POSITIONS else "Batter" for pos in out["pos"]]

    for column in INTEGER_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    out["age"] = pd.to_numeric(out["age"], errors="coerce")
    out["hard_hit_pct"] = pd.to_numeric(out["hard_hit_pct"], errors="coerce")
    out["tj_date"] = pd.to_datetime(out["tj_date"], format="%m/%d/%y", errors="coerce").dt.date
    out["player_id"] = [
        index.resolve(name, None if pd.isna(team) else team, player_type)
        for name, team, player_type in zip(out["name"], out["team"], out["player_type"])
    ]
    out = out.drop_duplicates(subset=["name", "team", "player_type", "fangraphs_id"], keep="first")
    return out[TABLE_COLUMNS].astype(object).where(out[TABLE_COLUMNS].notna(), None).reset_index(drop=True)


def import_prospects(path: Path, conn: db.Connection, snapshot: str, index: PlayerIndex) -> tuple[str, int, str, int]:
    """Insert one file as a new snapshot; returns (source, season, list type, rows)."""
    source, season, list_type = parse_filename(path.name)
    df = load_prospects(path, index)
    unmatched = df.loc[df["player_id"].isna(), "name"]
    if len(unmatched):
        print(f"Warning: {path.name}: no player id for {len(unmatched)} players, e.g. {unmatched.head(5).tolist()}",
              file=sys.stderr)
    columns = ["source", "season", "list_type", "snapshot", *df.columns]
    rows = [(source, season, list_type, snapshot, *row) for row in df.itertuples(index=False, name=None)]
    conn.executemany(
        f"INSERT INTO rankings_prospects ({', '.join(f'`{c}`' for c in columns)}) "  # noqa: S608
        f"VALUES ({', '.join(['%s'] * len(columns))})",
        rows,
    )
    return source, season, list_type, len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help=f"prospect files or folders (default: {DEFAULT_DIR})")
    args = parser.parse_args(argv)

    files, skipped = find_files(args.paths or [DEFAULT_DIR])
    for path in skipped:
        print(f"Skipping {path.name}: not a <source>-<year>-<type>.csv file name", file=sys.stderr)
    if not files:
        print("No prospect files found", file=sys.stderr)
        return 1

    snapshot = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with db.connect() as conn:
            conn.executescript(SCHEMA)
            index = PlayerIndex.load(conn)
            for path in files:
                source, season, list_type, count = import_prospects(path, conn, snapshot, index)
                print(f"{source} {season} {list_type}: {count} players")
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, db.Error, db.NotConfigured) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Imported {len(files)} prospect files as snapshot {snapshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

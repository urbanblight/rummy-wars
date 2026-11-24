#!/usr/bin/env python3
"""Import dynasty ranking CSVs into the ``rankings_dynasty`` table.

Usage:
    python scripts/import_rankings.py [PATH ...]

Each PATH is a ranking export or a folder of them (default: ``data/rankings``). Files are
named ``<source>-<season>-dynasty.csv``, for example ``bp-2026-dynasty.csv`` (Baseball
Prospectus); the source and season come from the name. The columns are ``Rank``, ``Change``
(places gained since the previous list; negative if the player dropped), ``Player``,
``Team`` and ``Pos``, plus an optional column named for the previous list (``July``)
holding the player's rank then. Batters and pitchers share one list; ``Pos`` of LHP/RHP/P
marks a pitcher. Run ``import_players.py`` first: each ``player_id`` is matched from the
name (and team) against the ``players`` table, NULL with a warning if unknown.

Every import is stored under a new ``snapshot`` (the UTC time it was inserted), so earlier
lists are kept; the latest snapshot of a source and season is the current ranking.
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
# Default folder scanned when no PATH is given.
DEFAULT_DIR = REPO_ROOT / "data" / "rankings"
# ``<source>-<season>-dynasty.csv``.
FILE_PATTERN = re.compile(r"^(.+)-(\d{4})-dynasty\.csv$", re.IGNORECASE)
# Positions that mark a pitcher, since batters and pitchers share one list.
PITCHER_POSITIONS = {"P", "SP", "RP", "LHP", "RHP"}
# Recognised headers (lower case); any other column is taken to be the previous list's rank.
KNOWN_COLUMNS = {"rank", "change", "player", "team", "pos"}

# DDL for ``rankings_dynasty``; one row per (source, season, snapshot, rank).
SCHEMA = """
CREATE TABLE IF NOT EXISTS rankings_dynasty (
    source VARCHAR(32) NOT NULL,
    season INT NOT NULL,
    snapshot VARCHAR(32) NOT NULL,
    `rank` INT NOT NULL,
    `change` INT,
    previous_rank INT,
    player_id VARCHAR(32),
    name VARCHAR(255) NOT NULL,
    team VARCHAR(32),
    pos VARCHAR(16),
    player_type VARCHAR(16) NOT NULL,
    UNIQUE KEY uq_rankings_dynasty (source, season, snapshot, `rank`),
    KEY idx_rankings_dynasty_player (player_id),
    FOREIGN KEY (player_id) REFERENCES players (player_id)
);
"""


def parse_filename(name: str) -> tuple[str, int]:
    """Read (source, season) from ``<source>-<season>-dynasty.csv``.

    Args:
        name: File name (not a path).

    Returns:
        ``(source lower-cased, season)``.

    Raises:
        ValueError: If the name does not match the pattern.
    """
    match = FILE_PATTERN.match(name)
    if not match:
        raise ValueError(f"{name}: expected <source>-<season>-dynasty.csv")
    return match.group(1).lower(), int(match.group(2))


def find_files(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Expand folders into their ranking files.

    Args:
        paths: Files and/or folders. Explicit files are taken as given; in folders only
            names matching ``FILE_PATTERN`` are used.

    Returns:
        ``(ranking files, skipped files)``.
    """
    files, skipped = [], []
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.glob("*.csv")):
                (files if FILE_PATTERN.match(candidate.name) else skipped).append(candidate)
        else:
            files.append(path)
    return files, skipped


def load_rankings(path: Path, index: PlayerIndex) -> pd.DataFrame:
    """Read a dynasty ranking export into table-shaped rows.

    Args:
        path: Ranking CSV. Headers are matched case-insensitively; an unrecognised extra
            column is read as the previous list's rank.
        index: Player lookup used to resolve ``player_id``.

    Returns:
        A DataFrame of rank, change, previous_rank, player_id, name, team, pos and
        player_type, with None for missing values. Rows lacking a rank or name are
        dropped and the first row wins for a repeated rank.

    Raises:
        ValueError: If the ``Rank`` or ``Player`` column is missing.
    """
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [str(column).strip() for column in df.columns]
    lower = {column.lower(): column for column in df.columns}
    missing = [column for column in ("rank", "player") if column not in lower]
    if missing:
        raise ValueError(f"{path.name}: missing column(s): {', '.join(missing)}")
    previous = next((column for column in df.columns if column.lower() not in KNOWN_COLUMNS), None)

    def column(name: str):
        return df[lower[name]] if name in lower else pd.Series(index=df.index, dtype=object)

    out = pd.DataFrame({
        "rank": pd.to_numeric(column("rank"), errors="coerce"),
        "change": pd.to_numeric(column("change"), errors="coerce"),
        "previous_rank": pd.to_numeric(df[previous], errors="coerce") if previous else None,
        "name": column("player").astype("string").str.strip(),
        "team": column("team").astype("string").str.strip(),
        "pos": column("pos").astype("string").str.strip(),
    })
    out = out[out["rank"].notna() & out["name"].notna()].copy()
    out["player_type"] = ["Pitcher" if str(pos).upper() in PITCHER_POSITIONS else "Batter" for pos in out["pos"]]
    out["player_id"] = [
        index.resolve(name, None if pd.isna(team) else team, player_type)
        for name, team, player_type in zip(out["name"], out["team"], out["player_type"])
    ]
    out = out.drop_duplicates(subset=["rank"], keep="first")
    out = out[["rank", "change", "previous_rank", "player_id", "name", "team", "pos", "player_type"]]
    for integer in ("rank", "change", "previous_rank"):
        out[integer] = out[integer].astype("Int64")
    return out.astype(object).where(out.notna(), None).reset_index(drop=True)


def import_rankings(path: Path, conn: db.Connection, snapshot: str, index: PlayerIndex) -> tuple[str, int, int]:
    """Insert one file as a new snapshot, warning on stderr about unmatched players.

    Args:
        path: Ranking CSV named ``<source>-<season>-dynasty.csv``.
        conn: Open database connection.
        snapshot: Timestamp shared by every file of one run.
        index: Player lookup used to resolve ids.

    Returns:
        ``(source, season, rows inserted)``.
    """
    source, season = parse_filename(path.name)
    df = load_rankings(path, index)
    unmatched = df.loc[df["player_id"].isna(), "name"]
    if len(unmatched):
        print(f"Warning: {path.name}: no player id for {len(unmatched)} players, e.g. {unmatched.head(5).tolist()}",
              file=sys.stderr)
    columns = ["source", "season", "snapshot", *df.columns]
    rows = [(source, season, snapshot, *row) for row in df.itertuples(index=False, name=None)]
    conn.executemany(
        f"INSERT INTO rankings_dynasty ({', '.join(f'`{c}`' for c in columns)}) "  # noqa: S608
        f"VALUES ({', '.join(['%s'] * len(columns))})",
        rows,
    )
    return source, season, len(rows)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        Exit status: 0 on success, 1 if no files were found or an import failed.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help=f"ranking files or folders (default: {DEFAULT_DIR})")
    args = parser.parse_args(argv)

    files, skipped = find_files(args.paths or [DEFAULT_DIR])
    for path in skipped:
        print(f"Skipping {path.name}: not a <source>-<season>-dynasty.csv file name", file=sys.stderr)
    if not files:
        print("No ranking files found", file=sys.stderr)
        return 1

    snapshot = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with db.connect() as conn:
            conn.executescript(SCHEMA)
            index = PlayerIndex.load(conn)
            for path in files:
                source, season, count = import_rankings(path, conn, snapshot, index)
                print(f"{source} {season} dynasty: {count} players")
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, db.Error, db.NotConfigured) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Imported {len(files)} ranking files as snapshot {snapshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

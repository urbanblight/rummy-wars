"""Import the league's fantasy teams from ``data/teams.csv`` into a sqlite database.

Usage:
    python scripts/import_teams.py [--csv data/teams.csv] [--db data/stats.db]

(Re)creates the ``teams`` table with an auto-assigned integer ``id`` per team
(1, 2, 3... in file order). Run this before ``import_cbs_stats.py``, which
stores these ids in the batting and pitching tables.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent

COLUMNS = {
    "TEAM NAME": "name",
    "TEAM ABBREV.": "abbrev",
    "MANAGER": "manager",
    "MANAGER EMAIL": "manager_email",
}


def normalize_team_name(name: str) -> str:
    """Name used to match teams across files (CBS exports backslash-escape quotes)."""
    return name.replace("\\", "").strip().casefold()


def load_teams(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, skiprows=2, index_col=False, dtype=str)
    df = df[list(COLUMNS)].rename(columns=COLUMNS)
    df = df[df["name"].notna()].copy()
    df = df.apply(lambda col: col.str.strip())
    df.insert(0, "id", range(1, len(df) + 1))
    return df.reset_index(drop=True)


def import_teams(csv_path: Path, db_path: Path) -> int:
    teams = load_teams(csv_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS teams")
        conn.execute(
            "CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "abbrev TEXT, manager TEXT, manager_email TEXT)"
        )
        teams.to_sql("teams", conn, if_exists="append", index=False)
    return len(teams)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=REPO_ROOT / "data" / "teams.csv")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "stats.db")
    args = parser.parse_args(argv)
    try:
        count = import_teams(args.csv, args.db)
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    print(f"Imported {count} teams into {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

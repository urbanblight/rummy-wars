"""Import the league's fantasy teams from ``data/teams.csv`` into the MySQL database.

Usage:
    python scripts/import_teams.py [--csv data/teams.csv]

Upserts the ``managers`` (matched by name, case-insensitively) and ``teams``
tables. Each team gets an integer ``id`` (1, 2, 4, 5... in file order, matching the
team ids CBS uses, which skip 3) and points
at its manager through ``manager_id``. Existing rows are updated in place, so
``teams_history`` is preserved. Run this before ``import_cbs_stats.py``, which
stores these ids in the batting and pitching tables.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import db  # noqa: E402

COLUMNS = {
    "TEAM NAME": "name",
    "TEAM ABBREV.": "abbrev",
    "MANAGER": "manager",
    "MANAGER EMAIL": "manager_email",
}


# CBS has no team 3, so ids after the second team are one higher than the file position.
CBS_SKIPPED_TEAM_ID = 3


def cbs_team_id(position: int) -> int:
    """Team id for the team at a 1-based position in the teams file."""
    return position if position < CBS_SKIPPED_TEAM_ID else position + 1


def normalize_team_name(name: str) -> str:
    """Name used to match teams across files (CBS exports backslash-escape quotes)."""
    return name.replace("\\", "").strip().casefold()


def load_teams(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, skiprows=2, index_col=False, dtype=str)
    df = df[list(COLUMNS)].rename(columns=COLUMNS)
    df = df[df["name"].notna()].copy()
    df = df.apply(lambda col: col.str.strip())
    df.insert(0, "id", [cbs_team_id(position) for position in range(1, len(df) + 1)])
    return df.reset_index(drop=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS managers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    email VARCHAR(255)
);
CREATE TABLE IF NOT EXISTS teams (
    id INT PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    abbrev VARCHAR(32),
    manager_id INT NOT NULL,
    FOREIGN KEY (manager_id) REFERENCES managers (id)
);
CREATE TABLE IF NOT EXISTS teams_history (
    team_id INT NOT NULL,
    season_year INT NOT NULL,
    name VARCHAR(255) NOT NULL,
    manager_id INT NOT NULL,
    PRIMARY KEY (team_id, season_year),
    UNIQUE (season_year, name),
    FOREIGN KEY (team_id) REFERENCES teams (id),
    FOREIGN KEY (manager_id) REFERENCES managers (id)
);
"""


def import_teams(csv_path: Path) -> int:
    teams = load_teams(csv_path)
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        for team in teams.itertuples(index=False):
            conn.execute(
                "INSERT INTO managers (name, email) VALUES (%s, %s) AS new "
                "ON DUPLICATE KEY UPDATE email = new.email",
                (team.manager, team.manager_email),
            )
            manager_id = conn.execute(
                "SELECT id FROM managers WHERE name = %s", (team.manager,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO teams (id, name, abbrev, manager_id) VALUES (%s, %s, %s, %s) AS new "
                "ON DUPLICATE KEY UPDATE name = new.name, "
                "abbrev = new.abbrev, manager_id = new.manager_id",
                (team.id, team.name, team.abbrev, manager_id),
            )
        placeholders = ",".join(["%s"] * len(teams))
        conn.execute(
            f"DELETE FROM teams WHERE id NOT IN ({placeholders})", teams["id"].tolist()
        )
    return len(teams)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=REPO_ROOT / "data" / "teams.csv")
    args = parser.parse_args(argv)
    try:
        count = import_teams(args.csv)
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    except (db.Error, db.NotConfigured) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Imported {count} teams")
    return 0


if __name__ == "__main__":
    sys.exit(main())

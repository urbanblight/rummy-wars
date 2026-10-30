"""Import a CBS league standings export into the MySQL database.

Usage:
    python scripts/import_standings.py data/standings/2025.csv [--year 2025]
        [--as-of 2025-09-28]

Accepts the tab-delimited paste (2023-2025) and the comma-delimited export
(2026). Each file holds the overall standings plus a per-category breakdown
(value, roto points and change in points). They are stored as one snapshot,
identified by ``season_year`` and ``as_of_date``, in two tables that a later
standings-gain-points (SGP) calculation can read:

* ``standings``: one row per team with batting/pitching/total points.
* ``standings_categories``: one row per team and scoring category.

``--year`` defaults to the number in the file name. ``--as-of`` defaults to
December 31 of the season, i.e. a full-year file; pass the export date for an
in-season file. Re-importing a snapshot replaces it. Teams are matched by name
against ``teams_history`` for the season (run ``import_teams.py`` first and
make sure the season's history exists).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import db  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS standings (
    season_year INT NOT NULL,
    as_of_date VARCHAR(10) NOT NULL,
    team_id INT NOT NULL,
    `rank` INT NOT NULL,
    batting_points DOUBLE NOT NULL,
    pitching_points DOUBLE NOT NULL,
    total_points DOUBLE NOT NULL,
    points_change DOUBLE,
    points_behind DOUBLE,
    PRIMARY KEY (season_year, as_of_date, team_id),
    FOREIGN KEY (team_id) REFERENCES teams (id)
);
CREATE TABLE IF NOT EXISTS standings_categories (
    season_year INT NOT NULL,
    as_of_date VARCHAR(10) NOT NULL,
    team_id INT NOT NULL,
    category VARCHAR(64) NOT NULL,
    category_type VARCHAR(16) NOT NULL CHECK (category_type IN ('batting', 'pitching')),
    value DOUBLE NOT NULL,
    points DOUBLE NOT NULL,
    points_change DOUBLE,
    PRIMARY KEY (season_year, as_of_date, team_id, category),
    FOREIGN KEY (team_id) REFERENCES teams (id)
);
"""


def normalize_team_name(name: str) -> str:
    """Match names across seasons: ignore case, punctuation, quote escapes and a leading 'The'."""
    name = re.sub(r"[^a-z0-9]", "", name.casefold().replace("\\", ""))
    return name[3:] if name.startswith("the") and len(name) > 3 else name


def split_line(line: str) -> list[str]:
    if "\t" in line:
        fields = [field.replace("\\", "") for field in line.split("\t")]
    else:
        fields = next(csv.reader([line], escapechar="\\"))
    fields = [field.strip() for field in fields]
    while fields and not fields[-1]:
        fields.pop()
    return fields


def number(value: str) -> float:
    return float(value)


def parse_standings(text: str) -> tuple[list[dict], list[dict]]:
    """Return (overall rows, category rows) keyed by raw team name."""
    overall: list[dict] = []
    categories: list[dict] = []
    section = None  # "overall" or "category"
    category_type = None
    category = None
    # Some exports glue a section heading onto the end of the previous row.
    text = re.sub(r"(?<=\S)(?=(?:Batting|Pitching) Breakdown)", "\n", text)
    for line in text.splitlines():
        fields = split_line(line)
        if not fields:
            section = None
            continue
        if len(fields) > 1 and fields[0].endswith("Rank") and fields[1] == "Team":
            section = "overall"
        elif fields[0] in ("Batting Breakdown", "Pitching Breakdown"):
            category_type = fields[0].split()[0].lower()
            section = None
        elif fields[0] == "Team" and len(fields) >= 3:
            category = fields[1]
            section = "category"
        elif section == "overall" and fields[0].isdigit() and len(fields) >= 7:
            rank, team, batting, pitching, total, change, behind = fields[:7]
            overall.append({
                "team": team, "rank": int(rank), "batting": number(batting),
                "pitching": number(pitching), "total": number(total),
                "change": number(change), "behind": number(behind),
            })
        elif section == "category" and len(fields) >= 4:
            team, value, points, change = fields[:4]
            categories.append({
                "team": team, "category": category, "type": category_type,
                "value": number(value), "points": number(points), "change": number(change),
            })
    return overall, categories


def load_team_ids(conn: db.Connection, year: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT team_id, name FROM teams_history WHERE season_year = %s", (year,)
    ).fetchall()
    if not rows:
        raise RuntimeError(f"No teams_history rows for {year}; add that season's teams first")
    return {normalize_team_name(name): team_id for team_id, name in rows}


def import_standings(path: Path, year: int, as_of: str) -> tuple[int, int]:
    overall, categories = parse_standings(path.read_text(encoding="utf-8-sig"))
    if not overall:
        raise ValueError(f"No overall standings found in {path}")
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        team_ids = load_team_ids(conn, year)
        unknown = sorted({r["team"] for r in overall + categories
                          if normalize_team_name(r["team"]) not in team_ids})
        if unknown:
            raise ValueError(f"Teams not in teams_history for {year}: {', '.join(unknown)}")
        key = (year, as_of)
        conn.execute("DELETE FROM standings WHERE season_year = %s AND as_of_date = %s", key)
        conn.execute(
            "DELETE FROM standings_categories WHERE season_year = %s AND as_of_date = %s", key
        )
        conn.executemany(
            "INSERT INTO standings VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(year, as_of, team_ids[normalize_team_name(r["team"])], r["rank"], r["batting"],
              r["pitching"], r["total"], r["change"], r["behind"]) for r in overall],
        )
        conn.executemany(
            "INSERT INTO standings_categories VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [(year, as_of, team_ids[normalize_team_name(r["team"])], r["category"], r["type"],
              r["value"], r["points"], r["change"]) for r in categories],
        )
    return len(overall), len(categories)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="standings export, e.g. data/standings/2025.csv")
    parser.add_argument("--year", type=int, help="season year (default: number in the file name)")
    parser.add_argument("--as-of", help="snapshot date YYYY-MM-DD (default: December 31 of the season)")
    args = parser.parse_args(argv)

    year = args.year
    if year is None:
        match = re.search(r"\d{4}", args.file.stem)
        if not match:
            parser.error("cannot infer the season from the file name; pass --year")
        year = int(match.group())
    as_of = args.as_of or f"{year}-12-31"
    try:
        date.fromisoformat(as_of)
    except ValueError:
        parser.error("--as-of must be a YYYY-MM-DD date")

    try:
        teams, rows = import_standings(args.file, year, as_of)
    except (FileNotFoundError, ValueError, RuntimeError, db.Error, db.NotConfigured) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Imported {year} standings as of {as_of}: {teams} teams, {rows} category rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())

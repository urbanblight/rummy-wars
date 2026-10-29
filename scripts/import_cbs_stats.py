"""Import CBS player-pool stats CSVs into a sqlite database.

Usage:
    python scripts/import_cbs_stats.py [--year 2026] [--data-dir data] [--db data/stats.db]

Reads ``<data-dir>/<year>/stats-batting.csv`` and ``stats-pitching.csv`` and
(re)creates the ``batting`` and ``pitching`` tables in the database (run
``import_teams.py`` and ``import_players.py`` first; each row's owner is stored as
``team_id``, NULL for free agents, and each player as the canonical ``player_id``
from the ``players`` table, NULL, with a warning, for names the ID map does not know), including
z-score and z-score rank columns for the league scoring categories (see
``zscores.save_z_scores``). Re-run after changing the scoring categories.

The SGP factors are recalculated from the imported standings first (see
``calculate_sgp.py``), so the SGP columns always use the latest standings.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

PLAYER_PATTERN = re.compile(r"^(.*?)\s+([A-Za-z0-9,]+)\s*\|\s*([A-Z]{2,3})$")
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import calculate_sgp  # noqa: E402
import projections  # noqa: E402
import zscores  # noqa: E402
from import_teams import normalize_team_name  # noqa: E402
from player_ids import PlayerIndex, normalize_name  # noqa: E402

load_dotenv(REPO_ROOT / ".env", override=False)


def innings_to_float(value: float) -> float:
    """Convert baseball notation (174.2 = 174 2/3 innings) to true innings."""
    whole = int(value)
    outs = round((value - whole) * 10)
    return whole + outs / 3


def load_team_ids(db_path: Path) -> dict[str, int]:
    """Map normalized fantasy team name -> teams.id (run import_teams.py first)."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT id, name FROM teams").fetchall()
    except sqlite3.OperationalError as error:
        raise RuntimeError(f"No teams table in {db_path}; run scripts/import_teams.py first") from error
    return {normalize_team_name(name): team_id for team_id, name in rows}


def load_player_index(db_path: Path) -> PlayerIndex:
    """Index the players table (run import_players.py first)."""
    with sqlite3.connect(db_path) as conn:
        return PlayerIndex.load(conn)


def load_csv(path: Path, team_ids: dict[str, int], index: PlayerIndex, player_type: str) -> pd.DataFrame:
    df = pd.read_csv(path, skiprows=1, index_col=False)
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    df = df[df["Player"].notna()].copy()  # drops the trailing "Report Updated" row
    parsed = df["Player"].str.strip().str.extract(PLAYER_PATTERN)
    df["name"] = parsed[0].fillna(df["Player"].str.strip())
    df["positions"] = parsed[1].fillna("")
    df["team"] = parsed[2].fillna("UNKNOWN")
    df["player_id"] = [index.resolve(n, t, player_type) for n, t in zip(df["name"], df["team"])]
    unmatched = df.loc[df["player_id"].isna(), "name"]
    if len(unmatched):
        print(f"Warning: no player id for {len(unmatched)} {player_type.lower()}s, e.g. {unmatched.head(5).tolist()}", file=sys.stderr)
    # Free agents (and anything not in the teams table) get a NULL team_id.
    df["team_id"] = df["Avail"].fillna("").map(lambda n: team_ids.get(normalize_team_name(n))).astype("Int64")
    unknown = sorted(set(df.loc[df["team_id"].isna() & (df["Avail"] != "FA"), "Avail"].dropna()))
    if unknown:
        print(f"Warning: no matching team for {unknown}", file=sys.stderr)
    df = df.rename(columns={"Rank": "rank"}).drop(columns=["Player", "Avail"])
    df["_key"] = df["player_id"].fillna(df["name"].map(normalize_name))
    df = df.sort_values("rank").drop_duplicates(subset=["_key", "team"], keep="first").drop(columns="_key")
    return df.reset_index(drop=True)


def import_stats(year: str, data_dir: Path, db_path: Path) -> dict[str, int]:
    team_ids = load_team_ids(db_path)
    index = load_player_index(db_path)
    batting = load_csv(data_dir / year / "stats-batting.csv", team_ids, index, "Batter")
    pitching = load_csv(data_dir / year / "stats-pitching.csv", team_ids, index, "Pitcher")
    pitching["INNs"] = pitching["INNs"].map(innings_to_float)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        batting.to_sql("batting", conn, if_exists="replace", index=False)
        pitching.to_sql("pitching", conn, if_exists="replace", index=False)
    try:
        calculate_sgp.save_factors(db_path)
    except (ValueError, sqlite3.Error) as error:
        print(f"Warning: SGP factors not updated ({error})", file=sys.stderr)
    zscores.save_z_scores(db_path)
    projections.save_projection_scores(db_path)  # replacement levels depend on who is a free agent now
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE INDEX idx_batting_player ON batting (player_id)")
        conn.execute("CREATE INDEX idx_pitching_player ON pitching (player_id)")
    return {"batting": len(batting), "pitching": len(pitching)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--year", default="2026")
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "stats.db")
    args = parser.parse_args(argv)
    try:
        counts = import_stats(args.year, args.data_dir, args.db)
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Imported {counts['batting']} batters and {counts['pitching']} pitchers into {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

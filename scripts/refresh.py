"""Refresh the whole stats database from the files under ``data/``.

Usage:
    python scripts/refresh.py [--year 2026] [--data-dir data]

Runs, in order:

1. ``import_teams.py``: teams and managers from ``<data-dir>/teams.csv``.
2. ``import_players.py``: the canonical player ID map, from ``<data-dir>/player-id-map.csv``
   if present, otherwise downloaded (safe to repeat; players are upserted).
3. ``import_standings.py``: every ``<data-dir>/standings/<year>.csv`` as that
   season's full-year snapshot (safe to repeat; a snapshot is replaced).
4. ``import_lineups.py``: every roster-overview file in ``<data-dir>/lineups`` (skipped
   if the folder is missing or has none; a lineup is replaced when imported again).
5. ``import_cbs_stats.py``: player stats for ``--year``. This first recalculates
   the SGP factors from the standings, then stores z-scores, SGP, the
   above-replacement values and their ranks.

Stops at the first step that fails. Teams that appear in a standings file must
already exist in ``teams_history`` for that season.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import db  # noqa: E402
import import_cbs_stats  # noqa: E402
import import_lineups  # noqa: E402
import import_players  # noqa: E402
import import_standings  # noqa: E402
import import_teams  # noqa: E402
from player_ids import PlayerIndex  # noqa: E402


def standings_files(data_dir: Path) -> list[tuple[int, Path]]:
    """Full-year standings files named ``<year>.csv``, oldest first."""
    files = [
        (int(path.stem), path)
        for path in (data_dir / "standings").glob("*.csv")
        if re.fullmatch(r"\d{4}", path.stem)
    ]
    return sorted(files)


def import_lineup_files(data_dir: Path) -> int:
    """Import the lineup files in ``<data_dir>/lineups`` in one transaction; returns how many."""
    folder = data_dir / "lineups"
    if not folder.is_dir():
        return 0
    files, skipped = import_lineups.find_files([folder])
    for path in skipped:
        print(f"Skipping {path.name}: not a roster-overview file name", file=sys.stderr)
    if not files:
        return 0
    with db.connect() as conn:
        import_lineups.ensure_schema(conn)
        index = PlayerIndex.load(conn)
        for path in files:
            import_lineups.import_lineup(path, conn, index)
    return len(files)


def refresh(year: str, data_dir: Path) -> None:
    count = import_teams.import_teams(data_dir / "teams.csv")
    print(f"Teams: imported {count} teams")

    players_csv = data_dir / "player-id-map.csv"
    source = players_csv if players_csv.exists() else import_players.DEFAULT_SOURCE
    print(f"Players: imported {import_players.import_players(source)} players")

    for season, path in standings_files(data_dir):
        teams, rows = import_standings.import_standings(path, season, f"{season}-12-31")
        print(f"Standings {season}: {teams} teams, {rows} category rows")

    lineups = import_lineup_files(data_dir)
    print(f"Lineups: imported {lineups} lineup files" if lineups else "Lineups: none found")

    counts = import_cbs_stats.import_stats(year, data_dir)
    print(f"Stats {year}: {counts['batting']} batters, {counts['pitching']} pitchers")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--year", default="2026", help="season of the player stats to import")
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    args = parser.parse_args(argv)
    try:
        refresh(args.year, args.data_dir)
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError, requests.RequestException, db.Error, db.NotConfigured) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print("Refreshed the stats database")
    return 0


if __name__ == "__main__":
    sys.exit(main())

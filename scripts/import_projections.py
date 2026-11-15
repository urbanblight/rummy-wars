#!/usr/bin/env python3
"""Import projection-system CSVs into the ``batting_projections`` and ``pitching_projections`` tables.

Usage:
    python scripts/import_projections.py [PATH ...]

Each PATH is a FanGraphs-style projection export or a folder of them (default:
``data/projections``). Files are named ``<system>-<season>-<batting|pitching>.csv``, for
example ``ZIPS-2027-batting.csv``; the system, season and player type come from the name.
Run ``import_players.py`` first: each row's ``player_id`` is the canonical id found from
the export's FanGraphs ``PlayerId`` (then ``MLBAMID``), NULL with a warning if unknown.

Every import is stored under a new ``snapshot`` (the UTC time it was inserted), so
projections are never overwritten and the history of a system's revisions is kept. Query
the latest with ``snapshot = (SELECT MAX(snapshot) ... WHERE `system` = %s AND season = %s)``.
After importing, z-scores, SGP and above-replacement values are calculated for every
system's latest projections (see ``projections.save_projection_scores``) and shown when the
projection is selected on a team page. Columns the export lacks are stored as NULL; columns
the tables do not list are ignored. Counting stats are stored as DOUBLE because Steamer
projects fractional totals (e.g. 145.09 games).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import db  # noqa: E402
import projections  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env", override=False)
# Default folder scanned when no PATH is given.
DEFAULT_DIR = REPO_ROOT / "data" / "projections"
# ``<system>-<season>-<batting|pitching>.csv``; the separator before the type may be ``-`` or ``.``.
FILE_PATTERN = re.compile(r"^(.+)-(\d{4})[-.](batting|pitching)\.csv$", re.IGNORECASE)

# Export column -> table column, per player type.
BATTING_COLUMNS = {
    "G": "g", "PA": "pa", "AB": "ab", "H": "h", "1B": "singles", "2B": "doubles", "3B": "triples",
    "HR": "hr", "R": "r", "RBI": "rbi", "BB": "bb", "IBB": "ibb", "SO": "so", "HBP": "hbp",
    "SF": "sf", "SH": "sh", "GDP": "gdp", "SB": "sb", "CS": "cs", "AVG": "avg", "BB%": "bb_pct",
    "K%": "k_pct", "OBP": "obp", "SLG": "slg", "wOBA": "woba", "OPS": "ops", "ISO": "iso",
    "BABIP": "babip", "wRC+": "wrc_plus", "WAR": "war", "ADP": "adp",
}
PITCHING_COLUMNS = {
    "W": "w", "L": "l", "ERA": "era", "GS": "gs", "G": "g", "QS": "qs", "SV": "sv", "HLD": "hld",
    "IP": "ip", "H": "h", "ER": "er", "HR": "hr", "SO": "so", "BB": "bb", "HBP": "hbp",
    "WHIP": "whip", "K/9": "k9", "BB/9": "bb9", "HR/9": "hr9", "K%": "k_pct", "BB%": "bb_pct",
    "AVG": "avg", "BABIP": "babip", "LOB%": "lob_pct", "FIP": "fip", "WAR": "war", "ADP": "adp",
}
# Player type -> its column map.
KINDS = {"batting": BATTING_COLUMNS, "pitching": PITCHING_COLUMNS}


def _schema(kind: str) -> str:
    """Build the CREATE TABLE statement for ``<kind>_projections`` from its column map.

    Args:
        kind: ``"batting"`` or ``"pitching"``.

    Returns:
        DDL with one DOUBLE column per distinct mapped stat. The unique key falls back to
        the name (via IFNULL) when a row has no FanGraphs id.
    """
    columns = ",\n    ".join(
        f"{column} DOUBLE"
        for column in dict.fromkeys(KINDS[kind].values())
    )
    return f"""
CREATE TABLE IF NOT EXISTS {kind}_projections (
    `system` VARCHAR(64) NOT NULL,
    season INT NOT NULL,
    snapshot VARCHAR(32) NOT NULL,
    player_id VARCHAR(32),
    fangraphs_id VARCHAR(32),
    mlb_id VARCHAR(32),
    name VARCHAR(255) NOT NULL,
    team VARCHAR(32),
    {columns},
    UNIQUE KEY uq_{kind}_projections_player (`system`, season, snapshot, (IFNULL(fangraphs_id, name))),
    KEY idx_{kind}_projections_player (player_id),
    FOREIGN KEY (player_id) REFERENCES players (player_id)
);
"""


# DDL for both projection tables.
SCHEMA = _schema("batting") + _schema("pitching")


def widen_count_columns(conn: db.Connection) -> None:
    """Convert INT stat columns left by earlier imports to DOUBLE.

    Steamer projects fractional counting stats (e.g. 145.09 games), which INT columns
    would round.

    Args:
        conn: Open database connection.
    """
    for kind, columns in KINDS.items():
        table = f"{kind}_projections"
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s AND data_type = 'int' AND column_name IN ({})".format(
                ", ".join(["%s"] * len(set(columns.values())))
            ),
            (table, *dict.fromkeys(columns.values())),
        ).fetchall()
        if rows:
            changes = ", ".join(f"MODIFY `{row[0]}` DOUBLE" for row in rows)
            conn.execute(f"ALTER TABLE {table} {changes}")  # noqa: S608


def parse_filename(name: str) -> tuple[str, int, str]:
    """Read (system, season, kind) from ``<system>-<season>-<batting|pitching>.csv``.

    Args:
        name: File name (not a path).

    Returns:
        ``(system, season, kind)`` with ``kind`` lower-cased.

    Raises:
        ValueError: If the name does not match the pattern.
    """
    match = FILE_PATTERN.match(name)
    if not match:
        raise ValueError(f"{name}: expected <system>-<season>-batting.csv or <system>-<season>-pitching.csv")
    system, season, kind = match.groups()
    return system, int(season), kind.lower()


def find_files(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Expand folders into their projection files.

    Args:
        paths: Files and/or folders. Explicit files are taken as given; in folders only
            names matching ``FILE_PATTERN`` are used.

    Returns:
        ``(projection files, skipped files)``.
    """
    files, skipped = [], []
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.glob("*.csv")):
                (files if FILE_PATTERN.match(candidate.name) else skipped).append(candidate)
        else:
            files.append(path)
    return files, skipped


def load_player_ids(conn: db.Connection) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Index the players table by FanGraphs id and by MLB id.

    Args:
        conn: Open database connection.

    Returns:
        ``(by_fangraphs, by_mlb)``, each mapping an external id to the list of canonical
        player ids using it (two-way players share one external id).

    Raises:
        RuntimeError: If the players table does not exist.
    """
    try:
        rows = conn.execute("SELECT player_id, fangraphs_id, mlb_id FROM players").fetchall()
    except db.SchemaError as error:
        raise RuntimeError("No players table in the database; run scripts/import_players.py first") from error
    by_fangraphs: dict[str, list[str]] = {}
    by_mlb: dict[str, list[str]] = {}
    for player_id, fangraphs_id, mlb_id in rows:
        if fangraphs_id:
            by_fangraphs.setdefault(fangraphs_id, []).append(player_id)
        if mlb_id:
            by_mlb.setdefault(mlb_id, []).append(player_id)
    return by_fangraphs, by_mlb


def pick(candidates: list[str] | None, kind: str) -> str | None:
    """Choose one player id from the candidates.

    Two-way players have a hitter id and a pitcher id ending in ``p``; the file's kind
    selects between them.

    Args:
        candidates: Player ids sharing an external id, or None.
        kind: ``"batting"`` or ``"pitching"``.

    Returns:
        The id, or None if there is none or it is still ambiguous.
    """
    if not candidates:
        return None
    if len(candidates) > 1:
        wanted = [c for c in candidates if c.endswith("p") == (kind == "pitching")]
        candidates = wanted or candidates
    return candidates[0] if len(candidates) == 1 else None


def _id_text(value) -> str | None:
    """Normalise an id cell: NaN/blank to None, and drop the ``.0`` pandas adds to floats."""
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text or None


def load_projections(path: Path, kind: str, ids: tuple[dict, dict]) -> pd.DataFrame:
    """Read a projection export into table-shaped rows.

    Args:
        path: Projection CSV.
        kind: ``"batting"`` or ``"pitching"``; selects the column map.
        ids: ``(by_fangraphs, by_mlb)`` from ``load_player_ids``.

    Returns:
        A DataFrame of the ``<kind>_projections`` columns (after system/season/snapshot),
        with missing values as None and duplicates by (FanGraphs id, name) removed.

    Raises:
        ValueError: If there is no ``Name`` column or the file lacks the columns that
            identify the kind (guards against a batting file named as pitching).
    """
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"PlayerId": str, "MLBAMID": str})
    if "Name" not in df.columns:
        raise ValueError(f"{path.name}: no Name column")
    df = df[df["Name"].notna()].copy()
    expected = [column for column in ("IP", "ERA") if kind == "pitching"] or ["PA", "AB"]
    if not any(column in df.columns for column in expected):
        raise ValueError(f"{path.name}: has no {'/'.join(expected)} column, so it does not look like a {kind} export")
    by_fangraphs, by_mlb = ids
    out = pd.DataFrame({
        "fangraphs_id": df.get("PlayerId", pd.Series(index=df.index, dtype=object)).map(_id_text),
        "mlb_id": df.get("MLBAMID", pd.Series(index=df.index, dtype=object)).map(_id_text),
        "name": df["Name"].str.strip(),
        "team": df["Team"].where(df["Team"].notna(), None) if "Team" in df else None,
    })
    out["player_id"] = [
        pick(by_fangraphs.get(fg), kind) or pick(by_mlb.get(mlb), kind)
        for fg, mlb in zip(out["fangraphs_id"], out["mlb_id"])
    ]
    for column, target in KINDS[kind].items():
        out[target] = pd.to_numeric(df[column], errors="coerce") if column in df else None
    out = out.drop_duplicates(subset=["fangraphs_id", "name"], keep="first")
    return out.astype(object).where(out.notna(), None).reset_index(drop=True)


def import_projections(path: Path, conn: db.Connection, snapshot: str, ids=None) -> tuple[str, int, str, int]:
    """Insert one file as a new snapshot, warning on stderr about unmatched players.

    Args:
        path: Projection CSV named ``<system>-<season>-<kind>.csv``.
        conn: Open database connection.
        snapshot: Timestamp shared by every file of one run.
        ids: Optional preloaded ``load_player_ids`` result.

    Returns:
        ``(system, season, kind, rows inserted)``.
    """
    system, season, kind = parse_filename(path.name)
    ids = ids or load_player_ids(conn)
    df = load_projections(path, kind, ids)
    unmatched = df.loc[df["player_id"].isna(), "name"]
    if len(unmatched):
        print(f"Warning: {path.name}: no player id for {len(unmatched)} players, e.g. {unmatched.head(5).tolist()}",
              file=sys.stderr)
    columns = ["`system`", "season", "snapshot", *df.columns]
    rows = [(system, season, snapshot, *row) for row in df.itertuples(index=False, name=None)]
    conn.executemany(
        f"INSERT INTO {kind}_projections ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",  # noqa: S608
        rows,
    )
    return system, season, kind, len(rows)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        Exit status: 0 on success, 1 if no files were found or an import failed. A
        failure to calculate projection scores afterwards is only a warning.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help=f"projection files or folders (default: {DEFAULT_DIR})")
    args = parser.parse_args(argv)

    files, skipped = find_files(args.paths or [DEFAULT_DIR])
    for path in skipped:
        print(f"Skipping {path.name}: not a <system>-<season>-batting/pitching.csv file name", file=sys.stderr)
    if not files:
        print("No projection files found", file=sys.stderr)
        return 1

    snapshot = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with db.connect() as conn:
            conn.executescript(SCHEMA)
            widen_count_columns(conn)
            ids = load_player_ids(conn)
            for path in files:
                system, season, kind, count = import_projections(path, conn, snapshot, ids)
                print(f"{system} {season} {kind}: {count} players")
    except FileNotFoundError as error:
        print(f"Missing input file: {error.filename}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, db.Error, db.NotConfigured) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    try:
        projections.save_projection_scores()
    except (ValueError, db.Error) as error:
        print(f"Warning: projection scores not calculated ({error})", file=sys.stderr)
    print(f"Imported {len(files)} projection files as snapshot {snapshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Resolve CBS player names to canonical player ids from the ``players`` table.

The ``players`` table is imported by ``scripts/import_players.py`` from the SFBB player ID
map. CBS exports only carry a name and an MLB team, so a name is matched against the map's
CBS and canonical names and then narrowed by MLB team, player type and active flag.
"""

from __future__ import annotations

import re
import unicodedata

try:
    from . import db
except ImportError:  # pragma: no cover - supports direct module imports
    import db

# CBS team abbreviation -> abbreviations the ID map may use for the same club.
TEAM_ALIASES = {
    "CHW": {"CHW", "CWS"},
    "WAS": {"WAS", "WSH", "WSN"},
    "SF": {"SF", "SFG"},
    "SD": {"SD", "SDP"},
    "TB": {"TB", "TBR"},
    "KC": {"KC", "KCR"},
    "ATH": {"ATH", "OAK"},
    "ARI": {"ARI", "AZ"},
}
SUFFIX = re.compile(r"\s+(jr|sr|ii|iii|iv)$")


def normalize_name(name: str) -> str:
    """Lowercase, accent-free, punctuation-free name used to match players."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", ascii_name.lower().replace("-", " ")).strip()


def name_keys(name: str | None) -> list[str]:
    """Lookup keys for a name: the normalized name, then the same without a Jr./II suffix."""
    if not name:
        return []
    full = normalize_name(name)
    stripped = SUFFIX.sub("", full)
    return [full] if stripped == full else [full, stripped]


class PlayerIndex:
    """Name lookup over the ``players`` table."""

    def __init__(self, rows: list[tuple]) -> None:
        self._by_key: dict[str, dict[str, tuple]] = {}
        self._by_last: dict[str, dict[str, tuple]] = {}
        for row in rows:
            player_id, name, cbs_name = row[0], row[1], row[2]
            for key in {k for value in (cbs_name, name) for k in name_keys(value)}:
                self._by_key.setdefault(key, {})[player_id] = tuple(row)
                first, _, last = key.partition(" ")
                if last:
                    self._by_last.setdefault(last, {})[player_id] = tuple(row)

    @classmethod
    def load(cls, conn: db.Connection) -> PlayerIndex:
        """Index the players table; raises RuntimeError if it has not been imported."""
        try:
            rows = conn.execute("SELECT player_id, name, cbs_name, team, pos, active FROM players").fetchall()
        except db.SchemaError as error:
            raise RuntimeError("No players table; run scripts/import_players.py first") from error
        return cls([tuple(row) for row in rows])

    def resolve(self, name: str, mlb_team: str | None = None, player_type: str | None = None) -> str | None:
        """The canonical player id for a CBS name, or None if unknown or still ambiguous."""
        candidates: dict[str, tuple] = {}
        for key in name_keys(name):
            candidates = self._by_key.get(key, {})
            if candidates:
                break
        rows = list(candidates.values())
        if not rows and mlb_team:
            rows = self._nickname_matches(name, mlb_team)
        if len(rows) > 1 and mlb_team:
            teams = TEAM_ALIASES.get(mlb_team, {mlb_team})
            rows = [r for r in rows if r[3] in teams] or rows
        if len(rows) > 1 and player_type:
            wanted = [r for r in rows if (r[4] == "P") == (player_type == "Pitcher")]
            rows = wanted or rows
        if len(rows) > 1:
            rows = [r for r in rows if r[5]] or rows
        if len(rows) > 1 and player_type:
            # Two-way players (Ohtani) have a hitter id and a pitcher id ending in "p".
            wanted = [r for r in rows if r[0].endswith("p") == (player_type == "Pitcher")]
            rows = wanted or rows
        return rows[0][0] if len(rows) == 1 else None

    def _nickname_matches(self, name: str, mlb_team: str) -> list[tuple]:
        """Same last name and team with a similar first name (Louie/Louis), for names CBS spells differently."""
        key = name_keys(name)[-1]
        first, _, last = key.partition(" ")
        teams = TEAM_ALIASES.get(mlb_team, {mlb_team})
        return [
            r for r in self._by_last.get(last, {}).values()
            if r[3] in teams and len(first) >= 3 and normalize_name(r[1]).startswith(first[:3])
        ] if last else []

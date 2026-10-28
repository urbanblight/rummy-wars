"""Data models for the Rummy Wars fantasy-baseball validation workflow.

These lightweight dataclasses describe a player, a roster, and the custom
application exception used throughout the project.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Player:
    """A single player record as parsed from a CBS fantasy roster export.

    This dataclass holds the roster slot, team, current status, player type,
    and any stat summary values needed during validation.
    """

    name: str
    roster_slot: str        # e.g., 'C', '1B', 'RP', 'P', 'Reserves'
    eligible_positions: list[str]
    team: str
    status: str             # 'Active', 'Reserves', 'Injured', 'Minors'
    player_type: str        # 'Batter' or 'Pitcher'
    stats: dict[str, float | None] = field(default_factory=dict)
    # Season values for the league's scoring categories, from the stats database.
    scoring_stats: dict[str, float | None] = field(default_factory=dict)
    z_scores: dict[str, float] = field(default_factory=dict)
    z_ranks: dict[str, int] = field(default_factory=dict)
    z_total: float | None = None
    z_total_rank: int | None = None
    sgp: dict[str, float] = field(default_factory=dict)
    sgp_ranks: dict[str, int] = field(default_factory=dict)
    sgp_total: float | None = None
    sgp_total_rank: int | None = None
    # {"total" or category: (value, rank)} for the player's best eligible position, or None.
    z_above_replacement: dict[str, tuple[float, int] | None] = field(default_factory=dict)
    sgp_above_replacement: dict[str, tuple[float, int] | None] = field(default_factory=dict)

    def advanced_payload(self) -> dict:
        """Z-score, SGP and above-replacement data in league category order, for the results-page modal."""

        def repl(values: dict, key: str) -> tuple[float | None, int | None]:
            return values.get(key) or (None, None)

        def entry(key: str, z, z_rank, sgp, sgp_rank) -> dict:
            z_repl, z_repl_rank = repl(self.z_above_replacement, key)
            sgp_repl, sgp_repl_rank = repl(self.sgp_above_replacement, key)
            return {
                "z": z, "z_rank": z_rank, "sgp": sgp, "sgp_rank": sgp_rank,
                "z_repl": z_repl, "z_repl_rank": z_repl_rank,
                "sgp_repl": sgp_repl, "sgp_repl_rank": sgp_repl_rank,
            }

        return {
            "total": entry("total", self.z_total, self.z_total_rank, self.sgp_total, self.sgp_total_rank),
            "cats": [
                {"cat": cat, **entry(cat, z, self.z_ranks.get(cat), self.sgp.get(cat), self.sgp_ranks.get(cat))}
                for cat, z in self.z_scores.items()
            ],
        }


@dataclass
class RummyWarsBaseError(Exception):
    """Base application exception with a standard message and optional error code.

    This exception is used to wrap validation and data-fetch errors in a
    consistent, readable format for logging and higher-level handling.
    """

    default_message: str = "An unexpected application error occurred."

    def __init__(self, message: str | None = None, code: str | None = None):
        self.message = message or self.default_message
        self.code = code
        super().__init__(self.message)

    def __str__(self) -> str:
        if self.code:
            return f"[{self.code}] {self.message}"
        return self.message

@dataclass
class TeamRoster:
    """Container for every player on a single fantasy roster.

    The roster object acts as a convenience wrapper for filtering and counting
    players by status (for example, Minors, Injured, or Active).
    """

    players: list[Player] = field(default_factory=list)

    def get_by_status(self, status: str) -> list[Player]:
        """Return every player whose status matches the provided value.

        Args:
            status: Status label to filter on, such as "Active" or "Minors".

        Returns:
            A list of matching Player objects.
        """
        return [p for p in self.players if p.status.lower() == status.lower()]

    def count_by_status(self, status: str) -> int:
        """Count the number of players in the requested roster status group.

        Args:
            status: Status label to count, such as "Injured".

        Returns:
            The number of players with that status.
        """
        return len(self.get_by_status(status))
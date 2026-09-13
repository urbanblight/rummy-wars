from dataclasses import dataclass, field


@dataclass
class Player:
    name: str
    roster_slot: str        # e.g., 'C', '1B', 'RP', 'P', 'Reserves'
    eligible_positions: list[str]
    team: str
    status: str             # 'Active', 'Reserves', 'Injured', 'Minors'
    player_type: str        # 'Batter' or 'Pitcher'
    stats: dict[str, float | None] = field(default_factory=dict)

@dataclass
class RummyWarsBaseError(Exception):
    """Base exception with support for custom messages and extra attributes."""

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
    players: list[Player] = field(default_factory=list)

    def get_by_status(self, status: str) -> list[Player]:
        return [p for p in self.players if p.status.lower() == status.lower()]

    def count_by_status(self, status: str) -> int:
        return len(self.get_by_status(status))
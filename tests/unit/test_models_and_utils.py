"""Unit tests for roster models and pure utility behavior.

These tests exercise logic that can run without a web server or network access.
External MLB lookups are replaced with mocks when a rule-validation test needs
to reach the validator's control flow.
"""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import models
import utils


class PlayerModelTests(unittest.TestCase):
    """Verify roster model filtering and application error formatting."""

    def test_roster_status_queries_are_case_insensitive(self):
        """Status filtering should ignore capitalization differences."""
        roster = models.TeamRoster([
            models.Player("One", "C", ["C"], "BOS", "Active", "Batter"),
            models.Player("Two", "MIN", ["1B"], "BOS", "Minors", "Batter"),
        ])

        self.assertEqual(roster.count_by_status("MINORS"), 1)
        self.assertEqual(
            [player.name for player in roster.get_by_status("active")],
            ["One"],
        )

    def test_base_error_formats_optional_code(self):
        """An error code should be included in the string representation."""
        error = models.RummyWarsBaseError("Bad roster", code="ROSTER")

        self.assertEqual(str(error), "[ROSTER] Bad roster")


class PlayerStringParsingTests(unittest.TestCase):
    """Verify conversion of CBS player labels into structured values."""

    def test_parses_name_positions_and_team(self):
        """A standard CBS label should produce all three player fields."""
        result = utils.parse_cbs_player_string("Caleb Durbin 2B,3B | BOS")

        self.assertEqual(result, ("Caleb Durbin", ["2B", "3B"], "BOS"))

    def test_returns_unknown_team_for_unrecognized_format(self):
        """Malformed labels should remain usable with safe fallback values."""
        result = utils.parse_cbs_player_string("Unrecognized player text")

        self.assertEqual(result, ("Unrecognized player text", [], "UNKNOWN"))


class RosterParsingTests(unittest.TestCase):
    """Verify CSV section tracking and batter/pitcher stat extraction."""

    def test_parses_sections_statuses_and_stats(self):
        """The parser should preserve roster type, status, and basic stats."""
        rows = [
            ["Batters"],
            ["", "Pos", "Players"],
            [
                "", "C", "Test Batter C | BOS", "", "", "", "", "",
                "0.250", "10", "2", "8", "1",
            ],
            ["Minors"],
            [
                "", "1B", "Test Minor 1B | SF", "", "", "", "", "",
                "N/A", "0", "0", "0", "0",
            ],
            ["Pitchers"],
            ["", "Pos", "Players"],
            [
                "", "P", "Test Pitcher P | NYY", "", "", "", "", "",
                "3.50", "1.20", "5", "0", "20",
            ],
        ]

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", newline="", delete=False
        ) as csv_file:
            csv.writer(csv_file).writerows(rows)
            csv_path = Path(csv_file.name)

        try:
            roster = utils.parse_cbs_roster_csv(csv_path)
        finally:
            csv_path.unlink()

        self.assertEqual(len(roster.players), 3)
        self.assertEqual(roster.players[0].status, "Active")
        self.assertEqual(roster.players[0].stats["HR"], 2)
        self.assertEqual(roster.players[1].status, "Minors")
        self.assertEqual(roster.players[1].stats["BA"], None)
        self.assertEqual(roster.players[2].player_type, "Pitcher")
        self.assertEqual(roster.players[2].stats["ERA"], 3.5)


class LeagueRuleUnitTests(unittest.TestCase):
    """Verify rule-limit reporting without making external MLB requests."""

    @patch.dict(
        "utils.os.environ",
        {"MAX_MINORS": "1", "MAX_IL": "1", "MAX_RESERVES": "1"},
    )
    @patch("utils.is_milb", return_value=True)
    @patch("utils.is_il", return_value=True)
    @patch("utils.get_mlbam_player_by_name", return_value={"id": 1})
    def test_uses_environment_rule_limits(self, *_mocks):
        """Unset rule limits should use configured environment values."""
        roster = models.TeamRoster([
            models.Player("Reserve One", "C", ["C"], "BOS", "Reserves", "Batter"),
            models.Player("Reserve Two", "C", ["C"], "BOS", "Reserves", "Batter"),
            models.Player("Minor One", "C", ["C"], "BOS", "Minors", "Batter"),
            models.Player("Minor Two", "C", ["C"], "BOS", "Minors", "Batter"),
            models.Player("Injured One", "C", ["C"], "BOS", "Injured", "Batter"),
            models.Player("Injured Two", "C", ["C"], "BOS", "Injured", "Batter"),
        ])

        violations, warnings = utils.validate_league_rules(roster)

        self.assertEqual(violations, [
            "Exceeded Reserve Slot Limit: 2/1",
            "Exceeded Minors Slot Limit: 2/1",
            "Exceeded Injured Reserve Limit: 2/1",
        ])
        self.assertEqual(warnings, [])

    @patch("utils.get_mlbam_player_by_name", return_value=None)
    def test_reports_minors_limit_without_calling_mlb(self, _get_player):
        """A Minors overage should be reported while lookups stay mocked."""
        roster = models.TeamRoster([
            models.Player("Minor One", "MIN", ["1B"], "BOS", "Minors", "Batter"),
            models.Player("Minor Two", "MIN", ["1B"], "BOS", "Minors", "Batter"),
        ])

        violations, warnings = utils.validate_league_rules(
            roster, max_minors=1, max_il=8
        )

        self.assertEqual(violations, ["Exceeded Minors Slot Limit: 2/1"])
        self.assertEqual(warnings, [])
        self.assertEqual(
            _get_player.call_args_list,
            [call("Minor One"), call("Minor Two")],
        )


if __name__ == "__main__":
    unittest.main() # pragma: no cover

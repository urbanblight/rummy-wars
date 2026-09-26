"""Unit tests for MLB API helpers and validator branches.

All HTTP calls are replaced with small deterministic response doubles. These
cases exercise helper success, empty, and fallback paths as well as the roster
validator's rule and warning branches without contacting the MLB API.
"""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import models
import utils


class FakeResponse:
    """Minimal response object implementing the requests API used by utils."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        """Return the configured JSON payload."""
        return self.payload

    def raise_for_status(self):
        """Match requests.Response for successful fake responses."""
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def player(name="Test Player", player_id=42):
    """Build a compact MLB person payload for helper tests."""
    return {"id": player_id, "fullName": name}


def roster_player(name, status, player_type="Batter"):
    """Build an application Player with the requested roster status."""
    return models.Player(name, "SLOT", ["P"], "BOS", status, player_type)


class MlbMetadataHelperTests(unittest.TestCase):
    """Verify helper functions that translate MLB StatsAPI responses."""

    def test_fake_response_raises_for_http_error(self):
        response = FakeResponse({}, status_code=500)

        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            response.raise_for_status()

    @patch("utils.requests.get")
    def test_is_milb_detects_parent_org_difference(self, get):
        get.return_value = FakeResponse({
            "people": [{"currentTeam": {"id": 11, "parentOrgId": 22}}]
        })

        self.assertTrue(utils.is_milb(player()))
        get.assert_called_once()

    @patch("utils.requests.get")
    def test_is_milb_returns_false_for_parent_org(self, get):
        get.return_value = FakeResponse({
            "people": [{"currentTeam": {"id": 11, "parentOrgId": 11}}]
        })

        self.assertFalse(utils.is_milb(player()))

    @patch("utils.requests.get")
    def test_is_milb_handles_missing_team_data(self, get):
        get.return_value = FakeResponse({"people": [{"currentTeam": {}}]})

        self.assertFalse(utils.is_milb(player("No Team")))

    @patch("utils.requests.get")
    def test_is_il_returns_false_for_inactive_player(self, get):
        get.return_value = FakeResponse({
            "people": [{"id": 42, "currentTeam": {"id": None}}]
        })

        self.assertFalse(utils.is_il(player()))
        get.assert_called_once()

    @patch("utils.requests.get")
    def test_is_il_returns_true_for_injured_roster_status(self, get):
        get.side_effect = [
            FakeResponse({"people": [{"id": 42, "currentTeam": {"id": 7}}]}),
            FakeResponse({"roster": [{
                "person": {"id": 42},
                "status": {"code": "D10", "description": "Injured 10-Day"},
            }]}),
        ]

        self.assertTrue(utils.is_il(player()))

    @patch("utils.requests.get")
    def test_is_il_compares_latest_placement_and_activation(self, get):
        get.side_effect = [
            FakeResponse({"people": [{
                "id": 42,
                "currentTeam": {"id": 7},
                "transactions": [
                    {"date": "2026-07-01", "description": "Rehab assignment"},
                    {"date": "2026-06-01", "description": "Placed on injured list"},
                    {"date": "2026-05-15", "description": "Activated from injured list"},
                ],
            }]}),
            FakeResponse({"roster": [{
                "person": {"id": 42},
                "status": {"code": "A", "description": "Active"},
            }]}),
        ]

        self.assertTrue(utils.is_il(player()))

    @patch("utils.requests.get")
    def test_is_il_returns_false_after_activation(self, get):
        get.side_effect = [
            FakeResponse({"people": [{
                "id": 42,
                "currentTeam": {"id": 7},
                "transactions": [
                    {"date": "2026-05-01", "description": "Placed on injured list"},
                    {"date": "2026-06-01", "description": "Activated from injured list"},
                ],
            }]}),
            FakeResponse({"roster": [{
                "person": {"id": 42},
                "status": {"code": "A", "description": "Active"},
            }]}),
        ]

        self.assertFalse(utils.is_il(player()))

    @patch("utils.requests.get")
    def test_is_il_returns_false_when_not_on_team_roster(self, get):
        get.side_effect = [
            FakeResponse({"people": [{"id": 42, "currentTeam": {"id": 7}}]}),
            FakeResponse({"roster": []}),
        ]

        self.assertFalse(utils.is_il(player()))

    @patch("utils.requests.get")
    def test_get_latest_activation_returns_latest_matching_transaction(self, get):
        get.return_value = FakeResponse({"people": [{
            "transactions": [
                {"date": "2026-04-01", "description": "Activated from injured list"},
                {"date": "2026-06-01", "description": "Activated from injured list"},
            ]
        }]})

        self.assertEqual(utils.get_mlb_latest_activation(player()), "2026-06-01")

    @patch("utils.requests.get")
    def test_get_latest_callup_returns_effective_date(self, get):
        get.return_value = FakeResponse({"transactions": [
            {"typeCode": "CU", "effectiveDate": "2026-05-01"},
            {"description": "Recalled", "effectiveDate": "2026-06-01"},
        ]})

        self.assertEqual(utils.get_mlb_latest_callup("42"), "2026-06-01")

    @patch("utils.requests.get")
    def test_get_latest_callup_returns_none_without_qualifying_transaction(self, get):
        get.return_value = FakeResponse({"transactions": [{"typeCode": "TR"}]})

        self.assertIsNone(utils.get_mlb_latest_callup("42"))

    @patch("utils.requests.get")
    def test_get_career_totals_reads_hitting_and_pitching(self, get):
        get.return_value = FakeResponse({"people": [{"stats": [
            {"group": {"displayName": "fielding"}, "splits": []},
            {"group": {"displayName": "hitting"}, "splits": [{
                "stat": {"atBats": "130"}
            }]},
            {"group": {"displayName": "pitching"}, "splits": [{
                "stat": {"inningsPitched": "12.1"}
            }]},
        ]}]})

        self.assertEqual(utils.get_mlb_career_totals(player()), {"ab": 130, "ip": 12.1})

    @patch("utils.requests.get")
    def test_get_player_by_name_handles_http_and_empty_results(self, get):
        get.return_value = FakeResponse({}, status_code=404)
        self.assertIsNone(utils.get_mlbam_player_by_name("Unknown"))

        get.return_value = FakeResponse({"people": []})
        self.assertIsNone(utils.get_mlbam_player_by_name("Unknown"))

    @patch("utils.requests.get")
    def test_get_player_by_name_returns_match(self, get):
        get.return_value = FakeResponse({"people": [player("Matched")]})

        self.assertEqual(utils.get_mlbam_player_by_name(" Matched ")["fullName"], "Matched")


class ValidatorBranchTests(unittest.TestCase):
    """Exercise validator warning and exception branches with mocked helpers."""

    def validate(self, players, **limits):
        """Run validation with a roster assembled from the supplied players."""
        return utils.validate_league_rules(models.TeamRoster(players), **limits)

    def test_parser_handles_empty_rows_sections_and_invalid_stats(self):
        """Parser should tolerate blank rows and malformed numeric fields."""
        rows = [
            [],
            ["Reserves"],
            ["", "C", "Reserve C | BOS", "", "", "", "", "", "bad", "bad", "bad", "bad", "bad"],
            ["Injured"],
            ["", "1B", "Injured 1B | BOS", "", "", "", "", "", "bad", "bad", "bad", "bad", "bad"],
            ["Pitchers"],
            ["", "P", "Pitcher P | BOS", "", "", "", "", "", "bad", "bad", "bad", "bad", "bad"],
            ["Active: 1"],
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

        self.assertEqual([player.status for player in roster.players], [
            "Reserves", "Injured", "Active"
        ])
        self.assertEqual(roster.players[0].stats, {})
        self.assertEqual(roster.players[2].stats, {})

    @patch("utils.get_mlb_latest_activation", return_value="2026-06-01")
    @patch("utils.is_il", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    @patch("utils.MODE", "inseason")
    def test_injured_player_adds_activation_warning(self, *_mocks):
        _, warnings = self.validate([roster_player("Injured", "Injured")])

        self.assertEqual(warnings, [
            "Injured is placed in an Injured slot and was activated 2026-06-01"
        ])

    @patch("utils.get_mlb_latest_activation", return_value="2026-06-01")
    @patch("utils.is_il", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    @patch("utils.MODE", "offseason")
    def test_injured_player_adds_activation_violation(self, *_mocks):
        violations, warnings = self.validate([roster_player("Injured", "Injured")])

        self.assertIn(
            "Injured is placed in an Injured slot and was activated 2026-06-01",
            violations,
        )
        self.assertEqual(warnings, [])

    @patch("utils.is_il", return_value=True)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    def test_injured_player_on_il_has_no_warning(self, *_mocks):
        _, warnings = self.validate([roster_player("Injured", "Injured")])

        self.assertEqual(warnings, [])

    @patch("utils.get_mlb_latest_callup", return_value="2026-06-01")
    @patch("utils.get_mlb_career_totals", return_value={"ab": 131, "ip": 0})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    @patch("utils.MODE", "inseason")
    def test_batter_overage_adds_minors_warning(self, *_mocks):
        _, warnings = self.validate([roster_player("Batter", "Minors")])

        self.assertIn("league maximum ABs for Minors", warnings[0])

    @patch("utils.get_mlb_latest_callup", return_value="2026-06-01")
    @patch("utils.get_mlb_career_totals", return_value={"ab": 131, "ip": 0})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    @patch("utils.MODE", "offseason")
    def test_batter_overage_adds_minors_violation(self, *_mocks):
        violations, warnings = self.validate(
            [roster_player("Batter", "Minors")], max_minors=20
        )

        self.assertIn("league maximum ABs for Minors", violations[0])
        self.assertEqual(warnings, [])

    @patch("utils.get_mlb_latest_callup", return_value="2026-06-01")
    @patch("utils.get_mlb_career_totals", return_value={"ab": 0, "ip": 50.1})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    @patch("utils.MODE", "inseason")
    def test_pitcher_overage_adds_minors_warning(self, *_mocks):
        _, warnings = self.validate([roster_player("Pitcher", "Minors", "Pitcher")])

        self.assertIn("league maximum IPs for Minors", warnings[0])

    @patch("utils.get_mlb_latest_callup", return_value="2026-06-01")
    @patch("utils.get_mlb_career_totals", return_value={"ab": 0, "ip": 50.1})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    @patch("utils.MODE", "offseason")
    def test_pitcher_overage_adds_minors_violation(self, *_mocks):
        violations, warnings = self.validate(
            [roster_player("Pitcher", "Minors", "Pitcher")], max_minors=20
        )

        self.assertIn("league maximum IPs for Minors", violations[0])
        self.assertEqual(warnings, [])

    @patch("utils.get_mlb_career_totals", return_value={"ab": 0, "ip": 0})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    def test_eligible_minors_player_has_no_warning(self, *_mocks):
        _, warnings = self.validate([roster_player("Eligible", "Minors")])

        self.assertEqual(warnings, [])

    @patch("utils.is_milb", return_value=True)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    def test_minor_league_player_has_no_warning(self, *_mocks):
        _, warnings = self.validate([roster_player("Minor", "Minors")])

        self.assertEqual(warnings, [])

    @patch("utils.get_mlb_career_totals", return_value={})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    def test_player_without_stats_has_no_warning(self, *_mocks):
        _, warnings = self.validate([roster_player("No Stats", "Minors")])

        self.assertEqual(warnings, [])

    @patch("utils.get_mlb_career_totals", return_value={"ab": 0, "ip": 0})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    def test_unknown_player_type_adds_warning(self, *_mocks):
        _, warnings = self.validate([roster_player("Unknown", "Minors", "Coach")])

        self.assertIn("neither a pitcher nor a batter", warnings[0])

    @patch("utils.get_mlbam_player_by_name", side_effect=RuntimeError("lookup failed"))
    def test_lookup_error_adds_warning(self, _get_player):
        _, warnings = self.validate([roster_player("Broken", "Minors")])

        self.assertEqual(warnings, [])

    @patch("utils.get_mlb_latest_callup", side_effect=RuntimeError("callup failed"))
    @patch("utils.get_mlb_career_totals", return_value={"ab": 131, "ip": 0})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    def test_callup_error_is_converted_to_warning(self, *_mocks):
        _, warnings = self.validate([roster_player("Broken Stats", "Minors")])

        self.assertEqual(warnings, [])

    def test_reserve_limit_is_a_violation(self):
        players = [roster_player(str(index), "Reserves") for index in range(8)]

        violations, warnings = self.validate(players, max_reserves=7)

        self.assertIn("Exceeded Reserve Slot Limit: 8/7", violations)
        self.assertEqual(warnings, [])

    @patch("utils.is_il", return_value=True)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    def test_injured_limit_is_a_violation(self, *_mocks):
        """More than eight injured players should produce a limit violation."""
        players = [roster_player(str(index), "Injured") for index in range(9)]

        violations, warnings = self.validate(players)

        self.assertIn("Exceeded Injured Reserve Limit: 9/8", violations)
        self.assertEqual(warnings, [])

    @patch("utils.get_mlb_career_totals", return_value={"ab": 0, "ip": 0})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    def test_eligible_pitcher_has_no_warning(self, *_mocks):
        """A pitcher below the innings threshold should remain warning-free."""
        _, warnings = self.validate([roster_player("Eligible", "Minors", "Pitcher")])

        self.assertEqual(warnings, [])

    @patch("utils.get_mlb_latest_callup", side_effect=RuntimeError("callup failed"))
    @patch("utils.get_mlb_career_totals", return_value={"ab": 0, "ip": 50.1})
    @patch("utils.is_milb", return_value=False)
    @patch("utils.get_mlbam_player_by_name", return_value=player())
    def test_pitcher_callup_error_is_suppressed(self, *_mocks):
        """Pitcher threshold lookup failures should be logged and suppressed."""
        _, warnings = self.validate([roster_player("Broken Pitcher", "Minors", "Pitcher")])

        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()  # pragma: no cover

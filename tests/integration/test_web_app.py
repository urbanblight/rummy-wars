"""Integration tests for the Flask roster evaluation workflow.

The tests exercise complete HTTP requests through Flask's test client. The
checked-in roster export is used for the successful upload case, while the
external MLB rule evaluation is mocked so the suite remains deterministic and
does not require network access.
"""

import io
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app, notices_by_player
import models


class WebAppIntegrationTests(unittest.TestCase):
    """Verify browser-facing routes and upload responses."""

    @classmethod
    def setUpClass(cls):
        """Create one test client and locate the shared CSV fixture."""
        cls.client = app.test_client()
        cls.sample_csv = (
            Path(__file__).parents[2] / "tests/fixtures/cbs-roster-export.csv"
        )

    @patch("app.MODE", "inseason")
    def test_home_page_contains_upload_and_loading_state(self):
        """The home page should expose upload controls and loading markup."""
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Upload a roster CSV', response.data)
        self.assertIn(b'id="loading-screen"', response.data)
        self.assertIn(b'aria-busy="true"', response.data)
        self.assertIn(b'<span class="ball">', response.data)
        self.assertNotIn(b'class="stovetop"', response.data)

    def test_home_page_links_to_teams(self):
        """The home page links to the teams page below the league links."""
        text = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="/teams"', text)
        self.assertLess(text.index("Constitution"), text.index('href="/teams"'))

    def test_teams_page_lists_teams_in_standings_order_with_links(self):
        """Each team links to its own players page, in the order the data layer returns."""
        snapshot = {"season_year": 2026, "as_of_date": "2026-12-31"}
        teams = [
            {"id": 7, "name": "The Outfield", "abbrev": "YL", "manager": "Matt B", "rank": 1, "total_points": 150.5, "active_sgp_vr": 42.5},
            {"id": 2, "name": "Jesus Jones", "abbrev": "RH", "manager": "Matt U", "rank": 2, "total_points": 120.0, "active_sgp_vr": None},
        ]
        with patch("app.league.league_teams", return_value=(snapshot, teams)):
            text = self.client.get("/teams").get_data(as_text=True)

        self.assertIn('href="/teams/7"', text)
        self.assertIn('href="/teams/2"', text)
        self.assertLess(text.index("The Outfield"), text.index("Jesus Jones"))
        self.assertIn("2026 standings", text)
        self.assertIn("Active SGP VR", text)
        self.assertIn("+42.50", text)

    def test_team_page_lists_the_teams_players_with_the_advanced_modal(self):
        """A team page shows its batters and pitchers using the shared player table."""
        team = {"id": 7, "name": "The Outfield", "abbrev": "YL", "manager": "Matt B", "rank": 1, "total_points": 150.5}
        snapshot = {"season_year": 2026, "as_of_date": "2026-12-31"}
        batter = models.Player(
            name="Test Batter", roster_slot="", eligible_positions=["C"], team="HOU", status="", player_type="Batter",
            scoring_stats={"HR": 20.0, "BA": 0.275}, cbs_rank=42,
            z_scores={"HR": 1.0}, z_ranks={"HR": 3}, z_total=1.0, z_total_rank=3,
            sgp_above_replacement={"total": (2.5, 9), "HR": (1.0, 4)},
        )
        with patch("app.league.get_team", return_value=team), \
                patch("app.league.league_teams", return_value=(snapshot, [team])), \
                patch("app.zscores.load_team_roster", return_value=(None, {"Batter": [batter], "Pitcher": []})):
            text = self.client.get("/teams/7").get_data(as_text=True)

        self.assertIn("The Outfield", text)
        self.assertIn("Test Batter", text)
        self.assertIn("Batters (1)", text)
        self.assertIn("+2.50", text)
        self.assertIn('<th class="num">CBS</th><th class="num">Rank</th><th class="num">SGP VR</th>', text)
        self.assertIn('<td class="num">42</td>', text)
        self.assertIn("BA&nbsp;.275", text)
        self.assertIn('id="adv-modal"', text)
        self.assertNotIn("<th>Slot</th>", text)
        self.assertNotIn("<th>Status</th>", text)

    def test_team_page_shows_the_lineup_slot_status_and_date_when_a_lineup_exists(self):
        """With a lineup the team page lists the slot and status columns and says when it is from."""
        team = {"id": 7, "name": "The Outfield", "abbrev": "YL", "manager": "Matt B", "rank": 1, "total_points": 150.5}
        snapshot = {"season_year": 2026, "as_of_date": "2026-12-31"}
        starter = models.Player(
            name="Lineup Starter", roster_slot="DH", eligible_positions=["1B"], team="TB", status="Active", player_type="Batter",
        )
        with patch("app.league.get_team", return_value=team), \
                patch("app.league.league_teams", return_value=(snapshot, [team])), \
                patch("app.zscores.load_team_roster", return_value=(
                    {"effective_date": "2026-09-28", "period_id": 12}, {"Batter": [starter], "Pitcher": []})):
            text = self.client.get("/teams/7").get_data(as_text=True)

        self.assertIn("Lineup as of 2026-09-28 (period 12)", text)
        self.assertIn("<th>Slot</th>", text)
        self.assertNotIn("<th>Status</th>", text)
        self.assertIn(">DH</td>", text)
        self.assertIn("Lineup Starter", text)

    def test_team_page_collapses_non_active_statuses(self):
        team = {"id": 7, "name": "The Outfield", "abbrev": "YL", "manager": "Matt B", "rank": 1, "total_points": 150.5}
        snapshot = {"season_year": 2026, "as_of_date": "2026-12-31"}
        players = [
            models.Player(name="Active Batter", roster_slot="C", eligible_positions=["C"], team="HOU", status="Active", player_type="Batter"),
            models.Player(name="Reserve Batter", roster_slot="Reserves", eligible_positions=["1B"], team="HOU", status="Reserves", player_type="Batter"),
            models.Player(name="Injured Batter", roster_slot="IL", eligible_positions=["OF"], team="HOU", status="Injured", player_type="Batter"),
            models.Player(name="Minor Leaguer", roster_slot="Minors", eligible_positions=["SS"], team="HOU", status="Minors", player_type="Batter"),
            models.Player(name="Active Pitcher", roster_slot="P", eligible_positions=["P"], team="HOU", status="Active", player_type="Pitcher"),
            models.Player(name="Reserve Pitcher", roster_slot="Reserves", eligible_positions=["P"], team="HOU", status="Reserves", player_type="Pitcher"),
            models.Player(name="Injured Pitcher", roster_slot="IL", eligible_positions=["P"], team="HOU", status="Injured", player_type="Pitcher"),
            models.Player(name="Minor League Pitcher", roster_slot="Minors", eligible_positions=["P"], team="HOU", status="Minors", player_type="Pitcher"),
        ]
        with patch("app.league.get_team", return_value=team), \
                patch("app.league.league_teams", return_value=(snapshot, [team])), \
                patch("app.zscores.load_team_roster", return_value=(
                    {"effective_date": "2026-09-28", "period_id": 12},
                    {"Batter": players[:4], "Pitcher": players[4:]},
                )):
            text = self.client.get("/teams/7").get_data(as_text=True)

        self.assertIn("Batters (1)", text)
        self.assertIn("Pitchers (1)", text)
        for player_type, status, player in (
            ("Batter", "Reserves", "Reserve Batter"),
            ("Batter", "Injured", "Injured Batter"),
            ("Batter", "Minors", "Minor Leaguer"),
            ("Pitcher", "Reserves", "Reserve Pitcher"),
            ("Pitcher", "Injured", "Injured Pitcher"),
            ("Pitcher", "Minors", "Minor League Pitcher"),
        ):
            marker = f'data-player-type="{player_type}" data-status="{status}"'
            accordion = text.split(marker, maxsplit=1)[1].split("</details>", maxsplit=1)[0]
            self.assertNotIn("<details open", text)
            self.assertIn(player, accordion)
        self.assertIn("Active Batter", text)
        self.assertIn("Active Pitcher", text)

    def test_team_page_offers_lineup_evaluation_only_with_a_lineup(self):
        team = {"id": 7, "name": "The Outfield", "abbrev": "YL", "manager": "Matt B", "rank": 1, "total_points": 150.5}
        snapshot = {"season_year": 2026, "as_of_date": "2026-12-31"}
        with_lineup = ({"effective_date": "2026-09-28", "period_id": None}, {"Batter": [], "Pitcher": []})
        for roster, expected in ((with_lineup, True), ((None, {"Batter": [], "Pitcher": []}), False)):
            with patch("app.league.get_team", return_value=team), \
                    patch("app.league.league_teams", return_value=(snapshot, [team])), \
                    patch("app.zscores.load_team_roster", return_value=roster):
                text = self.client.get("/teams/7").get_data(as_text=True)
            self.assertEqual('action="/teams/7/evaluate"' in text, expected)
            self.assertEqual('id="loading-screen"' in text, expected)

    def _lineup_roster(self):
        starter = models.Player(
            name="Lineup Starter", roster_slot="C", eligible_positions=["C"], team="TB", status="Active", player_type="Batter",
        )
        return {"effective_date": "2026-09-28", "period_id": None}, models.TeamRoster(players=[starter])

    def test_notices_are_grouped_by_the_player_they_are_about(self):
        violation = models.PlayerNotice("Ann Aaron is placed in an Injured slot", "Ann Aaron")
        warning = models.PlayerNotice("Ann Aaron has few ABs", "Ann Aaron")
        other = models.PlayerNotice("Bo Bell is in a Minors slot", "Bo Bell")

        grouped = notices_by_player([violation, "Exceeded Reserve Slot Limit: 9/7"], [warning, other, "A general note"])

        self.assertEqual(grouped, {
            "Ann Aaron": [
                {"kind": "violation", "message": "Ann Aaron is placed in an Injured slot"},
                {"kind": "warning", "message": "Ann Aaron has few ABs"},
            ],
            "Bo Bell": [{"kind": "warning", "message": "Bo Bell is in a Minors slot"}],
        })

    def test_results_show_an_icon_only_for_players_named_in_notices(self):
        violation = models.PlayerNotice("Yainer Diaz is placed in an Injured slot and was activated 2026-06-01", "Yainer Diaz")
        warnings = [models.PlayerNotice("Jake Bennett is in a Minors slot", "Jake Bennett"), "General warning"]
        with patch("app.utils.validate_league_rules", return_value=([violation], warnings)):
            with self.sample_csv.open("rb") as csv_file:
                response = self.client.post(
                    "/evaluate",
                    data={"roster": (csv_file, self.sample_csv.name)},
                    content_type="multipart/form-data",
                )

        text = response.get_data(as_text=True)
        self.assertEqual(text.count('class="note-icon"'), 2)
        self.assertIn('aria-label="Notifications for Yainer Diaz"', text)
        self.assertIn('aria-label="Notifications for Jake Bennett"', text)
        self.assertIn(">🚨</button>", text)
        self.assertIn(">⚠️</button>", text)
        self.assertIn('id="note-modal"', text)

    def test_evaluating_a_teams_lineup_shows_the_same_report_as_an_uploaded_roster(self):
        team = {"id": 7, "name": "The Outfield", "abbrev": "YL", "manager": "Matt B", "rank": 1, "total_points": 150.5}
        with patch("app.league.get_team", return_value=team), \
                patch("app.zscores.load_team_lineup", return_value=self._lineup_roster()), \
                patch("app.utils.validate_league_rules", return_value=(["Exceeded Minors Slot Limit: 21/20"], ["Review player eligibility"])) as validate:
            response = self.client.post("/teams/7/evaluate")

        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Roster report", text)
        self.assertIn("The Outfield lineup as of 2026-09-28 (offseason)", text)
        self.assertIn("1 rule violation found", text)
        self.assertIn("Exceeded Minors Slot Limit: 21/20", text)
        self.assertIn("Review player eligibility", text)
        self.assertIn("Lineup Starter", text)
        self.assertIn('href="/teams/7"', text)
        self.assertIn("Back to team", text)
        self.assertNotIn("Check another file", text)
        validate.assert_called_once()

    def test_evaluating_a_team_without_a_lineup_redirects_back_with_a_message(self):
        team = {"id": 7, "name": "The Outfield", "abbrev": "YL", "manager": "Matt B", "rank": 1, "total_points": 150.5}
        snapshot = {"season_year": 2026, "as_of_date": "2026-12-31"}
        with patch("app.league.get_team", return_value=team), \
                patch("app.league.league_teams", return_value=(snapshot, [team])), \
                patch("app.zscores.load_team_lineup", return_value=None), \
                patch("app.zscores.load_team_roster", return_value=(None, {"Batter": [], "Pitcher": []})):
            response = self.client.post("/teams/7/evaluate", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("No lineup has been imported for this team yet.", response.get_data(as_text=True))

    def test_a_failed_lineup_evaluation_redirects_back_with_a_message(self):
        team = {"id": 7, "name": "The Outfield", "abbrev": "YL", "manager": "Matt B", "rank": 1, "total_points": 150.5}
        snapshot = {"season_year": 2026, "as_of_date": "2026-12-31"}
        with patch("app.league.get_team", return_value=team), \
                patch("app.league.league_teams", return_value=(snapshot, [team])), \
                patch("app.zscores.load_team_lineup", return_value=self._lineup_roster()), \
                patch("app.zscores.load_team_roster", return_value=self._lineup_roster()[0:1] + ({"Batter": [], "Pitcher": []},)), \
                patch("app.utils.validate_league_rules", side_effect=RuntimeError("MLB API down")):
            response = self.client.post("/teams/7/evaluate", follow_redirects=True)

        self.assertIn("The lineup could not be evaluated.", response.get_data(as_text=True))

    def test_lineup_evaluation_is_post_only_and_needs_a_known_team(self):
        self.assertEqual(self.client.get("/teams/7/evaluate").status_code, 405)
        with patch("app.league.get_team", return_value=None):
            self.assertEqual(self.client.post("/teams/999/evaluate").status_code, 404)

    def test_unknown_team_returns_404(self):
        with patch("app.league.get_team", return_value=None):
            self.assertEqual(self.client.get("/teams/999").status_code, 404)

    @patch("app.MODE", "offseason")
    def test_offseason_loading_screen_uses_coffee_and_stovetop(self):
        """Offseason mode should show the coffee-and-stovetop loader."""
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="stovetop"', response.data)
        self.assertIn("☕".encode(), response.data)
        self.assertNotIn(b'<span class="ball">', response.data)

    def test_stat_value_filter_formats_missing_stats_as_placeholder(self):
        """Missing stat values should render as a dash in templates."""
        self.assertEqual(app.jinja_env.filters["stat_value"](None, "BA"), "-")

    def test_stat_value_filter_formats_each_scoring_stat(self):
        """Rate stats, innings and counting stats each get their usual format."""
        fmt = app.jinja_env.filters["stat_value"]
        self.assertEqual(fmt(0.2719, "BA"), ".272")
        self.assertEqual(fmt(0.4, "SLG"), ".400")
        self.assertEqual(fmt(3.456, "ERA"), "3.46")
        self.assertEqual(fmt(9.0, "Kd9"), "9.00")
        self.assertEqual(fmt(174.6666, "INN"), "174.7")
        self.assertEqual(fmt(31.0, "HR"), "31")

    def test_missing_upload_redirects_with_error(self):
        """A request without a file should return a flashed validation error."""
        response = self.client.post("/evaluate", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a CBS roster CSV to evaluate.", response.data)

    def test_non_csv_upload_redirects_with_error(self):
        """A non-CSV upload should be rejected before parsing begins."""
        response = self.client.post(
            "/evaluate",
            data={"roster": (io.BytesIO(b"not a roster"), "roster.txt")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Roster exports must be CSV files.", response.data)

    @patch("app.utils.parse_cbs_roster_csv", side_effect=ValueError("invalid CSV"))
    def test_unreadable_csv_redirects_with_error(self, _parse_roster):
        """Expected parser errors should be shown as upload feedback."""
        response = self.client.post(
            "/evaluate",
            data={"roster": (io.BytesIO(b"bad CSV"), "roster.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"That file could not be read as a CBS roster CSV.", response.data)

    @patch("app.utils.validate_league_rules", side_effect=RuntimeError("validation failed"))
    def test_unexpected_evaluation_error_redirects_with_error(self, _validate_rules):
        """Unexpected evaluation errors should return generic user feedback."""
        with self.sample_csv.open("rb") as csv_file:
            response = self.client.post(
                "/evaluate",
                data={"roster": (csv_file, self.sample_csv.name)},
                content_type="multipart/form-data",
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"The roster could not be evaluated.", response.data)

    @patch("flask.Flask.run")
    @patch.dict("os.environ", {"PORT": "9090"})
    def test_script_entrypoint_uses_configured_port(self, run):
        """Running app.py as a script should pass its configured port to Flask."""
        app_path = Path(__file__).parents[2] / "src/app.py"

        runpy.run_path(str(app_path), run_name="__main__")

        run.assert_called_once_with(host="127.0.0.1", port=9090)

    @patch("app.utils.validate_league_rules", return_value=([], []))
    def test_csv_upload_renders_evaluation_results(self, _validate_rules):
        """A valid CSV should render parsed players and evaluation results."""
        with self.sample_csv.open("rb") as csv_file:
            response = self.client.post(
                "/evaluate",
                data={"roster": (csv_file, self.sample_csv.name)},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        response_text = response.get_data(as_text=True)
        batters_section, pitchers_section = response_text.split(
            '<section aria-labelledby="batters-title">', maxsplit=1
        )[1].split(
            '<section aria-labelledby="pitchers-title">', maxsplit=1
        )
        pitchers_section = pitchers_section.split("</section>", maxsplit=1)[0]

        self.assertIn("Roster report", response_text)
        self.assertIn("Batters (10)", response_text)
        self.assertIn("Pitchers (10)", response_text)
        for title in (
            "Reserve batters (4)",
            "Injured batters (3)",
            "Minor league batters (12)",
            "Reserve pitchers (3)",
            "Injured pitchers (5)",
            "Minor league pitchers (8)",
        ):
            self.assertIn(f"<summary>{title}</summary>", response_text)
        self.assertNotIn("<th>Status</th>", response_text)
        self.assertIn("Yainer Diaz", batters_section)
        self.assertNotIn("Jake Bennett", batters_section)
        self.assertIn("Jake Bennett", pitchers_section)
        self.assertNotIn("Yainer Diaz", pitchers_section)
        _validate_rules.assert_called_once()

    @patch("app.utils.validate_league_rules", return_value=([], []))
    def test_results_page_shows_advanced_stats_modal(self, _validate_rules):
        """Players with scores get an advanced stats button carrying z-score and SGP data."""
        def attach_scores(roster):
            player = roster.players[0]
            player.z_scores, player.z_ranks, player.z_total, player.z_total_rank = {"HR": 1.5}, {"HR": 4}, 1.5, 4
            player.sgp, player.sgp_ranks, player.sgp_total, player.sgp_total_rank = {"HR": 2.0}, {"HR": 3}, 2.0, 3
            player.z_above_replacement = {"total": (0.5, 7), "HR": (0.5, 7)}
            player.sgp_above_replacement = {"total": (1.0, 6), "HR": (1.0, 6)}

        with patch("app.zscores.evaluate_roster", side_effect=attach_scores):
            with self.sample_csv.open("rb") as csv_file:
                response = self.client.post(
                    "/evaluate",
                    data={"roster": (csv_file, self.sample_csv.name)},
                    content_type="multipart/form-data",
                )

        text = response.get_data(as_text=True)
        self.assertIn("ADVANCED STATS", text)
        self.assertIn('id="adv-modal"', text)
        self.assertIn("Advanced stats for", text)
        self.assertIn('"sgp": 2.0', text)
        self.assertNotIn("LEAGUE Z-SCORES", text)
        self.assertIn('class="adv-info-tip"', text)
        self.assertIn('"z_repl": 0.5', text)
        self.assertIn("<th class=\"num\">SGP VR</th>", text)
        self.assertIn("+1.00", text)
        self.assertNotIn("adv-legend", text)

    @patch("app.utils.validate_league_rules", return_value=([], ["Review player eligibility"]))
    def test_csv_upload_renders_warnings(self, _validate_rules):
        """Warnings returned by validation should appear on the results page."""
        with self.sample_csv.open("rb") as csv_file:
            response = self.client.post(
                "/evaluate",
                data={"roster": (csv_file, self.sample_csv.name)},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Warnings", response.data)
        self.assertIn(b"Review player eligibility", response.data)
        _validate_rules.assert_called_once()


if __name__ == "__main__":
    unittest.main() # pragma: no cover

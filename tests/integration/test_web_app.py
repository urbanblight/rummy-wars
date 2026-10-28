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

from app import app


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

    def test_teams_route_is_removed(self):
        """The team totals page no longer exists."""
        self.assertEqual(self.client.get("/teams").status_code, 404)
        self.assertNotIn("/teams", self.client.get("/").get_data(as_text=True))

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
        self.assertIn("Batters (29)", response_text)
        self.assertIn("Pitchers (26)", response_text)
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


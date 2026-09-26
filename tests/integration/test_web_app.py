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

    @patch("app.MODE", "offseason")
    def test_offseason_loading_screen_uses_coffee_and_stovetop(self):
        """Offseason mode should show the coffee-and-stovetop loader."""
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="stovetop"', response.data)
        self.assertIn("☕".encode(), response.data)
        self.assertNotIn(b'<span class="ball">', response.data)

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
        app_path = Path(__file__).parents[2] / "app.py"

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

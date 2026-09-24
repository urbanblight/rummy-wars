"""Integration tests for the Flask roster evaluation workflow.

The tests exercise complete HTTP requests through Flask's test client. The
checked-in roster export is used for the successful upload case, while the
external MLB rule evaluation is mocked so the suite remains deterministic and
does not require network access.
"""

import io
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
            Path(__file__).parents[2] / "roster-overview-26-20260914.csv"
        )

    def test_home_page_contains_upload_and_loading_state(self):
        """The home page should expose upload controls and loading markup."""
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Upload a roster CSV', response.data)
        self.assertIn(b'id="loading-screen"', response.data)
        self.assertIn(b'aria-busy="true"', response.data)

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

    @patch("app.utils.validate_league_rules", return_value=[])
    def test_csv_upload_renders_evaluation_results(self, _validate_rules):
        """A valid CSV should render parsed players and evaluation results."""
        with self.sample_csv.open("rb") as csv_file:
            response = self.client.post(
                "/evaluate",
                data={"roster": (csv_file, self.sample_csv.name)},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Roster report", response.data)
        self.assertIn(b"Yainer Diaz", response.data)
        _validate_rules.assert_called_once()


if __name__ == "__main__":
    unittest.main() # pragma: no cover

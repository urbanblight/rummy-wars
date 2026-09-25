"""Tests for the command-line entry point and logger setup reuse."""

import runpy
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import logger
import main
import models


class MainTests(unittest.TestCase):
    """Verify CLI argument handling, reporting, and script execution."""

    def test_parse_arguments_reads_csv_path(self):
        args = main.parse_arguments(["--csv", "roster.csv"])

        self.assertEqual(args.csv, "roster.csv")

    @patch("main.LOGGER.info")
    @patch("main.utils.validate_league_rules", return_value=([], []))
    @patch("main.utils.parse_cbs_roster_csv", return_value=models.TeamRoster())
    @patch("main.parse_arguments", return_value=Namespace(csv="roster.csv"))
    def test_main_logs_when_roster_has_no_findings(
        self, _parse_args, _parse_roster, _validate_rules, log_info
    ):
        main.main()

        log_info.assert_any_call("Successfully loaded 0 players.")
        log_info.assert_any_call("Roster may be compliant with evaluated rules.")

    @patch("main.LOGGER.warning")
    @patch(
        "main.utils.validate_league_rules",
        return_value=(["over limit"], ["review player"]),
    )
    @patch("main.utils.parse_cbs_roster_csv", return_value=models.TeamRoster())
    @patch("main.parse_arguments", return_value=Namespace(csv="roster.csv"))
    def test_main_logs_violations_and_warnings(
        self, _parse_args, _parse_roster, _validate_rules, log_warning
    ):
        main.main()

        log_warning.assert_any_call("Rule Violations Detected:")
        log_warning.assert_any_call("- over limit")
        log_warning.assert_any_call("Warnings Detected:")
        log_warning.assert_any_call("- review player")

    def test_logger_reuses_existing_named_logger(self):
        self.assertIs(logger.setup_logger("main"), main.LOGGER)

    @patch(
        "utils.parse_cbs_roster_csv",
        side_effect=models.RummyWarsBaseError("bad roster"),
    )
    def test_script_entrypoint_swallows_application_errors(self, _parse_roster):
        main_path = Path(main.__file__)
        with patch.object(sys, "argv", [str(main_path), "--csv", "roster.csv"]):
            runpy.run_path(str(main_path), run_name="__main__")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
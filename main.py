"""Command-line entry point for validating a CBS fantasy roster.

This module orchestrates parsing a roster CSV and checking it against the rules
for minors and injured-list limits defined by the Rummy Wars league.
"""

import argparse

import logger
import models
import utils

LOGGER = logger.setup_logger("main")


def main(args_list=None):
    """Run the roster validation workflow from the command line.

    Args:
        args_list: Sequence of CLI arguments for testing or embedding.

    Returns:
        None. The program logs violations and exits through the CLI flow.
    """
    args = parse_arguments(args_list)
    roster = utils.parse_cbs_roster_csv(args.csv)
    LOGGER.info(f"Successfully loaded {len(roster.players)} players.")

    violations, warnings = utils.validate_league_rules(roster, max_minors=20, max_il=8)
    if violations:
        LOGGER.warning("Rule Violations Detected:")
        for v in violations:
            LOGGER.warning(f"- {v}")
    if warnings:
        LOGGER.warning("Warnings Detected:")
        for w in warnings:
            LOGGER.warning(f"- {w}")
if not violations and not warnings:
        LOGGER.info("Roster may be compliant with evaluated rules.")

def parse_arguments(args_list=None):
    """Parse and validate command-line arguments for the roster checker.

    Args:
        args_list: Argument list to parse instead of sys.argv.

    Returns:
        argparse.Namespace: Parsed CLI values including the required CSV path.
    """
    # Creating the parser object.
    parser = argparse.ArgumentParser(
        description="Evaluate downloaded CBS Fantasy Baseball CSV for a Rummy Wars team for violations"
    )

    # Defining required arguments.
    parser.add_argument(
        "-c",
        "--csv",
        type=str,
        help="path to the csv file",
        required=True)
    
    # Defining optional arguments.
    """
    parser.add_argument(
        "-t",
        "--team",
        type=str,
        help="Team name",
    )
    """
    # Parsing arguments from terminal.
    return parser.parse_args(args_list)

if __name__ == "__main__":
    try:
        main()
    except models.RummyWarsBaseError:
        pass
  

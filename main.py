import argparse

import logger
import models
import utils

LOGGER = logger.setup_logger("main")

def main(args_list=None):
    args = parse_arguments(args_list)
    roster = utils.parse_cbs_roster_csv(args.csv)
    LOGGER.info(f"Successfully loaded {len(roster.players)} players.")

    violations = utils.validate_league_rules(roster, max_minors=20, max_il=8)
    if violations:
        LOGGER.warning("Rule Violations Detected:")
        for v in violations:
            LOGGER.warning(f"- {v}")
    else:
        LOGGER.info("Roster may be compliant with evaluated rules, but check any warnings above.")

def parse_arguments(args_list=None):
    """
    Parse command-line arguments.
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
  

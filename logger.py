"""Logging configuration for the Rummy Wars application.

The project uses a single module-level logger setup so that other modules can
retrieve a consistent logger while avoiding duplicate handlers.
"""

import logging
import sys
from pathlib import Path

# Default logger name
LOGGER_NAME = "rummy-wars"
LOGGER_FILE_PATH = Path(__file__).resolve()
PROJECT_ROOT = LOGGER_FILE_PATH.parents[1]
LOG_DIR = PROJECT_ROOT / "logs"


def setup_logger(
    name: str = LOGGER_NAME,
) -> logging.Logger:
    """Create and configure a logger for console and file output.

    This helper ensures the logger is initialized once per unique name and
    attaches both a console handler and a file handler.

    Args:
        name: Logger name used when retrieving the logger instance.

    Returns:
        A configured logging.Logger instance ready for use in the application.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # Capture everything from DEBUG up

    # Avoid adding duplicate handlers if setup_logger is called multiple times
    if logger.handlers:
        return logger

    # Log format: Time - Name - Level - Message
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console Handler (Outputs INFO and above to terminal)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File Handler (Outputs DEBUG and above to app.log file)
    file_handler = logging.FileHandler('rummy-wars.log', encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

import logging
import sys
from pathlib import Path

# Default logger name
LOGGER_NAME = "rummy-wars"
LOGGER_FILE_PATH = Path(__file__).resolve()
PROJECT_ROOT = LOGGER_FILE_PATH.parents[1] 
LOG_DIR = PROJECT_ROOT / "logs"

def setup_logger(
    name: str = LOGGER_NAME
) -> logging.Logger:
    """Configures a logger that outputs to console"""
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
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger
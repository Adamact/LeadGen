"""Logging configuration for LeadGen."""

import logging
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"


def setup_logger(name: str = "leadgen", level: str = "DEBUG") -> logging.Logger:
    """
    Set up a logger that writes to both a timestamped file and stderr.

    Args:
        name: Logger name.
        level: Log level for file output (console is always INFO+).

    Returns:
        Configured logger instance.
    """
    LOG_DIR.mkdir(exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.DEBUG))

    # Avoid duplicate handlers on repeated calls
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — DEBUG level, one file per run
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_handler = logging.FileHandler(
        LOG_DIR / f"leadgen_{timestamp}.log",
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Stderr handler — WARNING+ only (rich handles INFO display)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    return logger


log = setup_logger()

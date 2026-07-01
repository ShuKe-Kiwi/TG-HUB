"""Logging configuration — structured, consistent across modules."""

import logging
import sys

from app.config import settings


def setup_logging() -> None:
    """Configure root logger with a simple stderr handler."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.DEBUG)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(name)

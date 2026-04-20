"""Logging configuration."""

import structlog
from pathlib import Path


def setup_logging():
    """Configure structlog for the application."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
    )

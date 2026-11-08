"""Local logging configuration (console + rotating file, never remote)."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_CONFIGURED = False


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("proofloop")
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "proofloop.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"proofloop.{name}")

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from absbot.config import Settings


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    log_file = Path(settings.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger(__name__).info(
        "Logging configured: level=%s file=%s max_bytes=%s backup_count=%s",
        logging.getLevelName(level),
        settings.log_file,
        settings.log_max_bytes,
        settings.log_backup_count,
    )

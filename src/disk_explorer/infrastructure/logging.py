"""Small structured JSON logger writing only to the user's application data."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from disk_explorer.config import user_data_dir


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        item: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ("operation", "target", "result"):
            value = getattr(record, field, None)
            if value is not None:
                item[field] = str(value)
        if record.exc_info:
            item["error"] = self.formatException(record.exc_info)
        return json.dumps(item, ensure_ascii=False)


def configure_logging(log_path: Path | None = None) -> logging.Logger:
    path = log_path or user_data_dir() / "logs" / "application.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("disk_explorer")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, "baseFilename", None) == str(path.resolve()) for handler in logger.handlers):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger

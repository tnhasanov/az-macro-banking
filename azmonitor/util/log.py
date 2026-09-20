"""Structured logging to stderr and a JSON-lines file in the data directory."""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from pathlib import Path


class JsonLineHandler(logging.Handler):
    def __init__(self, path: Path):
        super().__init__()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def emit(self, record: logging.LogRecord) -> None:
        payload = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if extra:
            payload["data"] = extra
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger("azmonitor")
    if root.handlers:
        return root
    root.setLevel(level)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(sh)
    if log_dir is not None:
        root.addHandler(JsonLineHandler(log_dir / f"monitor-{dt.date.today():%Y%m}.jsonl"))
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"azmonitor.{name}")

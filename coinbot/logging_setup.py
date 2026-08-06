"""Logging configuration: rotating file + console, optional JSON lines.

A trading bot's log is its audit trail, so secrets are scrubbed on the way out
and every record carries the module that produced it.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .config import LoggingConfig

# Anything that looks like a PEM body or a CDP key name gets masked.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"organizations/[0-9a-f-]+/apiKeys/[0-9a-f-]+"),
    re.compile(r"(?i)(api[_-]?secret|private[_-]?key|password|token)\"?\s*[:=]\s*\"?[^\s\",}]+"),
]


def scrub(text: str) -> str:
    """Replace anything that looks like a credential with a placeholder."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***redacted***", text)
    return text


class SecretFilter(logging.Filter):
    """Scrubs formatted messages before they reach any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        cleaned = scrub(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


class ConsoleFormatter(logging.Formatter):
    """Compact, colorized console output."""

    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;41m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        color = self.COLORS.get(record.levelname, "")
        return f"{color}{text}{self.RESET}" if color else text


class JsonFormatter(logging.Formatter):
    """One JSON object per line — handy for shipping logs somewhere else."""

    RESERVED = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(cfg: Optional[LoggingConfig] = None, *, verbose: bool = False) -> logging.Logger:
    """Install handlers on the root logger and return the bot's logger."""
    cfg = cfg or LoggingConfig()
    level = logging.DEBUG if verbose else getattr(logging, cfg.level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    secret_filter = SecretFilter()

    if cfg.console:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(
            JsonFormatter() if cfg.json_format else ConsoleFormatter(sys.stderr.isatty())
        )
        console.addFilter(secret_filter)
        root.addHandler(console)

    if cfg.file:
        path = Path(cfg.file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(
            JsonFormatter()
            if cfg.json_format
            else logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        file_handler.addFilter(secret_filter)
        root.addHandler(file_handler)

    # Third-party chatter drowns out the signal at DEBUG.
    for noisy in ("urllib3", "requests", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    return logging.getLogger("coinbot")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"coinbot.{name}")


__all__ = ["JsonFormatter", "get_logger", "scrub", "setup_logging"]

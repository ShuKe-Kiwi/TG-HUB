"""Production-safe structured logging with exclusive stream routing."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from app.config import settings

_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SAFE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_THIRD_PARTY_EVENTS = (
    ("uvicorn", "third_party.uvicorn", "UVICORN_WARNING"),
    ("sqlalchemy", "third_party.sqlalchemy", "SQLALCHEMY_WARNING"),
    ("telethon", "third_party.telethon", "TELETHON_WARNING"),
    ("httpx", "third_party.httpx", "HTTPX_WARNING"),
    ("httpcore", "third_party.httpx", "HTTPX_WARNING"),
)


def _third_party_mapping(logger_name: str) -> tuple[str, str] | None:
    for prefix, event, error_code in _THIRD_PARTY_EVENTS:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return event, error_code
    return None


class StructuredJsonFormatter(logging.Formatter):
    """Build JSON only from an explicit allowlist; never format arbitrary args."""

    def format(self, record: logging.LogRecord) -> str:
        mapping = _third_party_mapping(record.name)
        raw_event = record.msg if isinstance(record.msg, str) else ""
        if mapping is not None:
            event, default_error = mapping
        elif _EVENT_PATTERN.fullmatch(raw_event):
            event, default_error = raw_event, None
        else:
            event, default_error = "logging.invalid_event", "LOG_EVENT_INVALID"

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": event,
        }
        error_code = getattr(record, "error_code", None) or default_error
        if isinstance(error_code, str) and _ERROR_CODE_PATTERN.fullmatch(error_code):
            payload["error_code"] = error_code
        for field in ("request_id", "runtime_state"):
            value = getattr(record, field, None)
            if isinstance(value, str) and _SAFE_VALUE_PATTERN.fullmatch(value):
                payload[field] = value
        retry_count = getattr(record, "retry_count", None)
        if isinstance(retry_count, int) and not isinstance(retry_count, bool) and retry_count >= 0:
            payload["retry_count"] = retry_count
        recoverable = getattr(record, "recoverable", None)
        if isinstance(recoverable, bool):
            payload["recoverable"] = recoverable
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


class StdoutLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        return record.levelno == logging.ERROR and getattr(record, "recoverable", False) is True


class StderrLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.ERROR:
            return True
        return record.levelno == logging.ERROR and getattr(record, "recoverable", False) is not True


def _handler(stream: TextIO, filter_: logging.Filter) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.addFilter(filter_)
    handler.setFormatter(StructuredJsonFormatter())
    return handler


def setup_logging() -> None:
    """Configure mutually exclusive stdout/stderr handlers without file I/O."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(_handler(sys.stdout, StdoutLogFilter()))
    root.addHandler(_handler(sys.stderr, StderrLogFilter()))

    logging.getLogger("uvicorn.access").disabled = True
    for name in ("telethon", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

import json
import logging

from app.infra.logger import (
    StderrLogFilter,
    StdoutLogFilter,
    StructuredJsonFormatter,
)


def _record(
    level: int,
    msg: object,
    *,
    name: str = "app.test",
    **extra: object,
) -> logging.LogRecord:
    record = logging.LogRecord(name, level, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _payload(record: logging.LogRecord) -> dict[str, object]:
    return json.loads(StructuredJsonFormatter().format(record))


def test_formatter_emits_only_allowlisted_structured_fields() -> None:
    record = _record(
        logging.ERROR,
        "ingestion.failed",
        error_code="DATABASE_UNAVAILABLE",
        recoverable=True,
        retry_count=2,
        request_id="req-123",
        secret="must-not-appear",
    )

    assert _payload(record) | {} == {
        "timestamp": _payload(record)["timestamp"],
        "level": "ERROR",
        "logger": "app.test",
        "event": "ingestion.failed",
        "error_code": "DATABASE_UNAVAILABLE",
        "request_id": "req-123",
        "retry_count": 2,
        "recoverable": True,
    }
    assert "must-not-appear" not in StructuredJsonFormatter().format(record)


def test_invalid_event_args_and_exception_are_not_rendered() -> None:
    seeded = "bot-token:123456:SECRET database=postgresql://secret"
    record = logging.LogRecord(
        "app.test", logging.ERROR, __file__, 1, "failed %s", (seeded,), RuntimeError(seeded)
    )
    rendered = StructuredJsonFormatter().format(record)

    assert seeded not in rendered
    assert _payload(record)["event"] == "logging.invalid_event"
    assert _payload(record)["error_code"] == "LOG_EVENT_INVALID"


def test_error_stream_filters_are_mutually_exclusive() -> None:
    cases = [
        (_record(logging.INFO, "app.started"), True, False),
        (_record(logging.WARNING, "app.warning"), True, False),
        (_record(logging.ERROR, "app.failed", recoverable=True), True, False),
        (_record(logging.ERROR, "app.failed"), False, True),
        (_record(logging.CRITICAL, "app.failed", recoverable=True), False, True),
    ]
    stdout_filter = StdoutLogFilter()
    stderr_filter = StderrLogFilter()
    for record, stdout_expected, stderr_expected in cases:
        assert bool(stdout_filter.filter(record)) is stdout_expected
        assert bool(stderr_filter.filter(record)) is stderr_expected


def test_third_party_message_is_replaced_by_stable_event() -> None:
    record = _record(
        logging.WARNING,
        "request failed token=SECRET",
        name="telethon.network.mtprotosender",
    )
    rendered = StructuredJsonFormatter().format(record)

    assert "SECRET" not in rendered
    assert _payload(record)["event"] == "third_party.telethon"
    assert _payload(record)["error_code"] == "TELETHON_WARNING"

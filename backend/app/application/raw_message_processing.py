"""RawMessage processing application boundary.

P6-2F coordinates existing RawMessage parser/dedup service methods without
putting business processing inside monitor runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.schema import (
    RawMessageProcessingErrorCode,
    RawMessageProcessingResult,
    RawMessageProcessingStatus,
)
from app.database import async_session_factory
from app.modules.rawmessage.service import RawMessageService

_FINAL_DEDUP_STATUSES = {"new", "matched", "skipped"}
_Action = Literal["already_processed", "parse_failed", "parse", "dedup"]


class _SessionContext(Protocol):
    async def __aenter__(self) -> AsyncSession: ...

    async def __aexit__(self, exc_type, exc, tb) -> object: ...


class _RawMessageServiceLike(Protocol):
    async def get_by_id(self, raw_msg_id: int) -> Any: ...

    async def parse_and_persist(self, raw_msg_id: int) -> Any: ...

    async def dedup_and_persist(
        self,
        raw_msg_id: int,
        event_bus: Any | None = None,
    ) -> Any: ...


SessionFactory = Callable[[], _SessionContext]
RawMessageServiceFactory = Callable[[AsyncSession], _RawMessageServiceLike]


class RawMessageProcessingBoundary:
    """Application-owned RawMessage parser/normalizer/dedup orchestration."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = async_session_factory,
        raw_message_service_factory: RawMessageServiceFactory = RawMessageService,
    ) -> None:
        self._session_factory = session_factory
        self._raw_message_service_factory = raw_message_service_factory

    async def process_raw_message(
        self,
        raw_message_id: int | str,
    ) -> RawMessageProcessingResult:
        parsed_id = _parse_raw_message_id(raw_message_id)
        if isinstance(parsed_id, RawMessageProcessingResult):
            return parsed_id

        async with self._session_factory() as session:
            service = self._raw_message_service_factory(session)
            raw_message = await self._load_raw_message(service, parsed_id)
            if isinstance(raw_message, RawMessageProcessingResult):
                return raw_message
            if raw_message is None:
                return _result(
                    status="raw_message_not_found",
                    raw_message_id=parsed_id,
                    error_code="RAW_MESSAGE_NOT_FOUND",
                )

            action = _action_for_state(raw_message)
            if isinstance(action, RawMessageProcessingResult):
                return _copy_state(action, raw_message_id=parsed_id, raw_message=raw_message)
            if action == "already_processed":
                return _result_from_raw_message(
                    raw_message,
                    status="already_processed",
                )
            if action == "parse_failed":
                return _result_from_raw_message(
                    raw_message,
                    status="parse_failed",
                    error_code="RAW_MESSAGE_PARSE_ERROR",
                )
            if action == "parse":
                parsed = await self._parse(service, parsed_id)
                if isinstance(parsed, RawMessageProcessingResult):
                    return parsed
                if not _is_expected_raw_message(parsed, parsed_id):
                    return _result(
                        status="processing_failed",
                        raw_message_id=parsed_id,
                        error_code="INVALID_SERVICE_RESULT",
                    )
                if _parse_status(parsed) == "parse_failed":
                    return _result_from_raw_message(
                        parsed,
                        status="parse_failed",
                        error_code="RAW_MESSAGE_PARSE_ERROR",
                        parse_executed=True,
                    )
                if (
                    _parse_status(parsed) != "parsed"
                    or _dedup_status(parsed) != "dedup_pending"
                ):
                    return _result_from_raw_message(
                        parsed,
                        status="processing_failed",
                        error_code="INVALID_SERVICE_RESULT",
                        parse_executed=True,
                    )
                return await self._dedup(
                    service,
                    parsed_id,
                    parse_executed=True,
                )

            return await self._dedup(service, parsed_id, parse_executed=False)

    async def _load_raw_message(
        self,
        service: _RawMessageServiceLike,
        raw_message_id: int,
    ) -> Any | RawMessageProcessingResult | None:
        try:
            return await service.get_by_id(raw_message_id)
        except LookupError:
            return _result(
                status="raw_message_not_found",
                raw_message_id=raw_message_id,
                error_code="RAW_MESSAGE_NOT_FOUND",
            )
        except SQLAlchemyError:
            return _result(
                status="processing_failed",
                raw_message_id=raw_message_id,
                error_code="DATABASE_ERROR",
            )
        except Exception:
            return _result(
                status="processing_failed",
                raw_message_id=raw_message_id,
                error_code="RAW_MESSAGE_PROCESSING_ERROR",
            )

    async def _parse(
        self,
        service: _RawMessageServiceLike,
        raw_message_id: int,
    ) -> Any | RawMessageProcessingResult:
        try:
            return await service.parse_and_persist(raw_message_id)
        except LookupError:
            return _result(
                status="raw_message_not_found",
                raw_message_id=raw_message_id,
                error_code="RAW_MESSAGE_NOT_FOUND",
                parse_executed=True,
            )
        except SQLAlchemyError:
            return _result(
                status="processing_failed",
                raw_message_id=raw_message_id,
                error_code="DATABASE_ERROR",
                parse_executed=True,
            )
        except Exception:
            return _result(
                status="parse_failed",
                raw_message_id=raw_message_id,
                error_code="RAW_MESSAGE_PARSE_ERROR",
                parse_executed=True,
            )

    async def _dedup(
        self,
        service: _RawMessageServiceLike,
        raw_message_id: int,
        *,
        parse_executed: bool,
    ) -> RawMessageProcessingResult:
        try:
            raw_message = await service.dedup_and_persist(
                raw_message_id,
                event_bus=None,
            )
        except SQLAlchemyError:
            return _result(
                status="processing_failed",
                raw_message_id=raw_message_id,
                error_code="DATABASE_ERROR",
                parse_executed=parse_executed,
                dedup_executed=True,
            )
        except Exception:
            return _result(
                status="dedup_failed",
                raw_message_id=raw_message_id,
                error_code="RAW_MESSAGE_DEDUP_ERROR",
                parse_executed=parse_executed,
                dedup_executed=True,
            )

        if not _is_expected_raw_message(raw_message, raw_message_id):
            return _result(
                status="processing_failed",
                raw_message_id=raw_message_id,
                error_code="INVALID_SERVICE_RESULT",
                parse_executed=parse_executed,
                dedup_executed=True,
            )
        if _parse_status(raw_message) != "parsed":
            return _result_from_raw_message(
                raw_message,
                status="processing_failed",
                error_code="INVALID_SERVICE_RESULT",
                parse_executed=parse_executed,
                dedup_executed=True,
            )

        dedup_status = _dedup_status(raw_message)
        if dedup_status == "new":
            return _result_from_raw_message(
                raw_message,
                status="dedup_new",
                parse_executed=parse_executed,
                dedup_executed=True,
            )
        if dedup_status == "matched":
            return _result_from_raw_message(
                raw_message,
                status="dedup_matched",
                parse_executed=parse_executed,
                dedup_executed=True,
            )
        if dedup_status == "skipped":
            return _result_from_raw_message(
                raw_message,
                status="dedup_skipped",
                parse_executed=parse_executed,
                dedup_executed=True,
            )
        if dedup_status == "dedup_pending":
            return _result_from_raw_message(
                raw_message,
                status="dedup_failed",
                error_code="RAW_MESSAGE_DEDUP_ERROR",
                parse_executed=parse_executed,
                dedup_executed=True,
            )

        return _result_from_raw_message(
            raw_message,
            status="processing_failed",
            error_code="INVALID_SERVICE_RESULT",
            parse_executed=parse_executed,
            dedup_executed=True,
        )


def _parse_raw_message_id(
    raw_message_id: int | str,
) -> int | RawMessageProcessingResult:
    if isinstance(raw_message_id, bool):
        return _result(
            status="invalid_raw_message_id",
            raw_message_id=None,
            error_code="RAW_MESSAGE_ID_NOT_INTEGER",
        )
    try:
        value = int(str(raw_message_id).strip())
    except ValueError:
        return _result(
            status="invalid_raw_message_id",
            raw_message_id=None,
            error_code="RAW_MESSAGE_ID_NOT_INTEGER",
        )
    if value <= 0:
        return _result(
            status="invalid_raw_message_id",
            raw_message_id=None,
            error_code="RAW_MESSAGE_ID_NON_POSITIVE",
        )
    return value


def _action_for_state(raw_message: Any) -> _Action | RawMessageProcessingResult:
    parse_status = _parse_status(raw_message)
    dedup_status = _dedup_status(raw_message)
    if parse_status == "parse_pending" and dedup_status == "dedup_pending":
        return "parse"
    if parse_status == "parsed" and dedup_status == "dedup_pending":
        return "dedup"
    if parse_status == "parsed" and dedup_status in _FINAL_DEDUP_STATUSES:
        return "already_processed"
    if parse_status == "parse_failed" and dedup_status == "dedup_pending":
        return "parse_failed"
    return _result(
        status="processing_failed",
        raw_message_id=_raw_message_id(raw_message),
        parse_status=parse_status,
        dedup_status=dedup_status,
        parse_attempts=_parse_attempts(raw_message),
        parsed_resource_count=_parsed_resource_count(raw_message),
        error_code="RAW_MESSAGE_INVALID_STATE",
    )


def _is_expected_raw_message(raw_message: Any, expected_id: int) -> bool:
    return _raw_message_id(raw_message) == expected_id


def _raw_message_id(raw_message: Any) -> int | None:
    value = getattr(raw_message, "id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_status(raw_message: Any) -> str | None:
    value = getattr(raw_message, "parse_status", None)
    return value if isinstance(value, str) else None


def _dedup_status(raw_message: Any) -> str | None:
    value = getattr(raw_message, "dedup_status", None)
    return value if isinstance(value, str) else None


def _parse_attempts(raw_message: Any) -> int | None:
    value = getattr(raw_message, "parse_attempts", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parsed_resource_count(raw_message: Any) -> int:
    parsed_data = getattr(raw_message, "parsed_data", None)
    if isinstance(parsed_data, list):
        return len(parsed_data)
    return 0


def _result_from_raw_message(
    raw_message: Any,
    *,
    status: RawMessageProcessingStatus,
    error_code: RawMessageProcessingErrorCode | None = None,
    parse_executed: bool = False,
    dedup_executed: bool = False,
) -> RawMessageProcessingResult:
    return _result(
        status=status,
        raw_message_id=_raw_message_id(raw_message),
        parse_status=_parse_status(raw_message),
        dedup_status=_dedup_status(raw_message),
        parse_attempts=_parse_attempts(raw_message),
        parsed_resource_count=_parsed_resource_count(raw_message),
        error_code=error_code,
        parse_executed=parse_executed,
        dedup_executed=dedup_executed,
    )


def _copy_state(
    result: RawMessageProcessingResult,
    *,
    raw_message_id: int,
    raw_message: Any,
) -> RawMessageProcessingResult:
    return result.model_copy(
        update={
            "raw_message_id": raw_message_id,
            "parse_status": _parse_status(raw_message),
            "dedup_status": _dedup_status(raw_message),
            "parse_attempts": _parse_attempts(raw_message),
            "parsed_resource_count": _parsed_resource_count(raw_message),
        }
    )


def _result(
    *,
    status: RawMessageProcessingStatus,
    raw_message_id: int | None,
    parse_status: str | None = None,
    dedup_status: str | None = None,
    parse_attempts: int | None = None,
    parsed_resource_count: int = 0,
    error_code: RawMessageProcessingErrorCode | None = None,
    parse_executed: bool = False,
    dedup_executed: bool = False,
) -> RawMessageProcessingResult:
    return RawMessageProcessingResult(
        status=status,
        raw_message_id=raw_message_id,
        parse_status=parse_status,
        dedup_status=dedup_status,
        parse_attempts=parse_attempts,
        parsed_resource_count=parsed_resource_count,
        error_code=error_code,
        parse_executed=parse_executed,
        dedup_executed=dedup_executed,
    )

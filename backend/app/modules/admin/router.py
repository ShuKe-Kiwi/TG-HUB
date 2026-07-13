"""Local-only Admin API for watchlist and monitor control boundaries."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.modules.monitor.control import MonitorControlError
from app.modules.monitor.watchlist_service import WatchlistServiceError
from app.deploy.rotation_status import invalid_rotation_projection

_API_VERSION = "v1"
_SCHEMA_VERSION = 1
_ADMIN_PREFIX = "/api/admin/v1"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class AdminApiError(RuntimeError):
    def __init__(self, error_code: str, status_code: int) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code


class MonitorStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_watchlist_revision: str | None = None


class WatchlistReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: str | None = None
    recovery_confirmed: bool = False
    config: dict[str, Any]


router = APIRouter(prefix=_ADMIN_PREFIX)


def _request_id(request: Request) -> str:
    return getattr(request.state, "admin_request_id", "unavailable")


def _envelope(request: Request, data: Any) -> dict[str, Any]:
    return {
        "api_version": _API_VERSION,
        "schema_version": _SCHEMA_VERSION,
        "request_id": _request_id(request),
        "data": data,
    }


def _error_envelope(request: Request, error_code: str) -> dict[str, Any]:
    return {
        "api_version": _API_VERSION,
        "schema_version": _SCHEMA_VERSION,
        "request_id": _request_id(request),
        "error": {"code": error_code},
    }


async def admin_error_handler(
    request: Request,
    exc: AdminApiError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_envelope(request, exc.error_code),
        headers={"Cache-Control": "no-store"},
    )


async def admin_request_validation_handler(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    if request.url.path.startswith(_ADMIN_PREFIX):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_envelope(request, "INVALID_REQUEST"),
            headers={"Cache-Control": "no-store"},
        )
    return await request_validation_exception_handler(request, exc)


def guard_admin_request(request: Request) -> None:
    hostname = request.url.hostname
    client_host = request.client.host if request.client is not None else None
    if hostname not in _LOCAL_HOSTS or client_host not in _LOCAL_HOSTS:
        raise AdminApiError(
            "ADMIN_ORIGIN_REJECTED",
            status.HTTP_403_FORBIDDEN,
        )
    host = request.headers.get("host", "")
    origin = request.headers.get("origin", "").rstrip("/")
    expected_origin = f"{request.url.scheme}://{host}"
    if not host or (origin and origin != expected_origin):
        raise AdminApiError(
            "ADMIN_ORIGIN_REJECTED",
            status.HTTP_403_FORBIDDEN,
        )
    if request.method not in _MUTATION_METHODS:
        return
    if not origin:
        raise AdminApiError(
            "ADMIN_ORIGIN_REJECTED",
            status.HTTP_403_FORBIDDEN,
        )
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/json"):
        raise AdminApiError(
            "INVALID_REQUEST",
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    provided = request.headers.get("X-TG-Hub-CSRF", "")
    expected = request.app.state.admin_csrf_token
    if not provided or not secrets.compare_digest(provided, expected):
        raise AdminApiError(
            "ADMIN_CSRF_REJECTED",
            status.HTTP_403_FORBIDDEN,
        )


def _control_service(request: Request):
    return request.app.state.monitor_control_service


def _watchlist_service(request: Request):
    return request.app.state.watchlist_service


def _rotation_status_reader(request: Request):
    return request.app.state.rotation_status_reader


@router.get("/monitor/status")
async def monitor_status(request: Request) -> dict[str, Any]:
    snapshot = await _control_service(request).status()
    return _envelope(request, snapshot.model_dump(mode="json"))


@router.get("/observability/rotation")
async def rotation_status(request: Request) -> dict[str, Any]:
    try:
        projection = await asyncio.to_thread(_rotation_status_reader(request))
    except Exception:
        projection = invalid_rotation_projection()
    return _envelope(request, projection.model_dump(mode="json"))


@router.post("/monitor/preflight")
async def monitor_preflight(request: Request) -> dict[str, Any]:
    try:
        report = await _control_service(request).run_preflight()
    except MonitorControlError as exc:
        raise _map_control_error(exc) from None
    return _envelope(request, report.model_dump(mode="json"))


@router.post("/monitor/start", status_code=status.HTTP_202_ACCEPTED)
async def monitor_start(
    request: Request,
    body: MonitorStartRequest,
) -> dict[str, Any]:
    try:
        result = await _control_service(request).start(
            body.expected_watchlist_revision
        )
    except MonitorControlError as exc:
        raise _map_control_error(exc) from None
    return _envelope(request, result.model_dump(mode="json"))


@router.post("/monitor/stop")
async def monitor_stop(request: Request) -> Response:
    try:
        result = await _control_service(request).stop()
    except MonitorControlError as exc:
        raise _map_control_error(exc) from None
    payload = _envelope(request, result.model_dump(mode="json"))
    response_status = (
        status.HTTP_409_CONFLICT
        if result.status == "timeout"
        else status.HTTP_200_OK
    )
    return JSONResponse(status_code=response_status, content=payload)


@router.get("/watchlist")
async def get_watchlist(request: Request) -> dict[str, Any]:
    snapshot = await _watchlist_service(request).get_snapshot()
    data = snapshot.model_dump(mode="json")
    data["watchlist_schema_version"] = snapshot.watchlist_schema_version
    data["validation"] = {
        "valid": snapshot.status == "valid",
        "error_code": snapshot.error_code,
    }
    return _envelope(request, data)


@router.put("/watchlist")
async def replace_watchlist(
    request: Request,
    body: WatchlistReplaceRequest,
) -> dict[str, Any]:
    try:
        result = await _watchlist_service(request).replace(
            body.config,
            body.expected_revision,
            recovery_confirmed=body.recovery_confirmed,
        )
    except WatchlistServiceError as exc:
        raise _map_watchlist_error(exc) from None
    return _envelope(request, result.model_dump(mode="json"))


def _map_control_error(exc: MonitorControlError) -> AdminApiError:
    if exc.error_code in {
        "MONITOR_ALREADY_ACTIVE",
        "MONITOR_CONTROL_BUSY",
        "MONITOR_TASK_STILL_RUNNING",
        "MONITOR_STATE_CONFLICT",
    }:
        return AdminApiError(exc.error_code, status.HTTP_409_CONFLICT)
    if exc.error_code in {
        "MONITOR_PRECHECK_FAILED",
        "WATCHLIST_NOT_READY",
    }:
        return AdminApiError(
            exc.error_code,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return AdminApiError(
        exc.error_code,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _map_watchlist_error(exc: WatchlistServiceError) -> AdminApiError:
    if exc.error_code == "WATCHLIST_SCHEMA_INVALID":
        return AdminApiError(
            exc.error_code,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if exc.error_code in {
        "WATCHLIST_REVISION_CONFLICT",
        "WATCHLIST_CURRENT_FILE_INVALID",
        "WATCHLIST_NOT_FOUND",
        "WATCHLIST_PATH_UNSAFE",
    }:
        return AdminApiError(exc.error_code, status.HTTP_409_CONFLICT)
    return AdminApiError(
        exc.error_code,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )

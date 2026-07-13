"""Monitor heartbeat sinks with isolated, bounded persistence."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.modules.monitor.runtime import MonitorHeartbeat

MAX_HEARTBEAT_LINE_BYTES = 64 * 1024
DEFAULT_FLOCK_TIMEOUT_SECONDS = 1.0
FLOCK_POLL_SECONDS = 0.01

PersistenceState = Literal[
    "disabled",
    "idle",
    "ok",
    "write_failed",
    "permission_denied",
    "path_invalid",
    "lock_timeout",
    "short_write",
    "closed",
]


class HeartbeatPersistenceStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    status: PersistenceState
    error_code: str | None = None


class HeartbeatSink(Protocol):
    async def emit(self, heartbeat: MonitorHeartbeat) -> None: ...

    async def aclose(self) -> None: ...


class NullHeartbeatSink:
    error_count = 0
    last_error_code: str | None = None

    async def emit(self, heartbeat: MonitorHeartbeat) -> None:
        return None

    async def aclose(self) -> None:
        return None


class CompositeHeartbeatSink:
    """Call ordered sinks while isolating ordinary sink failures."""

    def __init__(self, *sinks: HeartbeatSink) -> None:
        self.sinks = tuple(sinks)
        self._isolated_error_count = 0
        self._isolated_error_code: str | None = None
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def emit(self, heartbeat: MonitorHeartbeat) -> None:
        for sink in self.sinks:
            errors_before = int(getattr(sink, "error_count", 0))
            try:
                await sink.emit(heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception:
                if int(getattr(sink, "error_count", 0)) <= errors_before:
                    self._isolated_error_count += 1
                self._isolated_error_code = getattr(
                    sink, "last_error_code", None
                ) or "HEARTBEAT_WRITE_FAILED"

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            for sink in self.sinks:
                errors_before = int(getattr(sink, "error_count", 0))
                try:
                    await sink.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if int(getattr(sink, "error_count", 0)) <= errors_before:
                        self._isolated_error_count += 1
                    self._isolated_error_code = getattr(
                        sink, "last_error_code", None
                    ) or "HEARTBEAT_WRITE_FAILED"

    @property
    def error_count(self) -> int:
        return self._isolated_error_count + sum(
            int(getattr(sink, "error_count", 0)) for sink in self.sinks
        )

    @property
    def last_error_code(self) -> str | None:
        if self._isolated_error_code is not None:
            return self._isolated_error_code
        for sink in reversed(self.sinks):
            code = getattr(sink, "last_error_code", None)
            if code is not None:
                return code
        return None

    def statuses(self) -> tuple[HeartbeatPersistenceStatus, ...]:
        statuses: list[HeartbeatPersistenceStatus] = []
        for sink in self.sinks:
            status_getter = getattr(sink, "status", None)
            if callable(status_getter):
                status = status_getter()
                if isinstance(status, HeartbeatPersistenceStatus):
                    statuses.append(status)
        return tuple(statuses)


class _WriteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: PersistenceState
    error_code: str | None = None


class _ShortWriteError(OSError):
    pass


class _LockTimeoutError(TimeoutError):
    pass


class _PathInvalidError(OSError):
    pass


class JsonlHeartbeatSink:
    """Append one bounded JSONL heartbeat using a short-lived file handle."""

    def __init__(
        self,
        path: str | Path,
        *,
        flock_timeout_seconds: float = DEFAULT_FLOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.parent / "heartbeat.lock"
        self.flock_timeout_seconds = max(flock_timeout_seconds, 0.0)
        self.error_count = 0
        self.last_error_code: str | None = None
        self.closed = False
        self._lock = asyncio.Lock()
        self._status = HeartbeatPersistenceStatus(
            enabled=True,
            status="idle",
        )

    def status(self) -> HeartbeatPersistenceStatus:
        return self._status

    async def emit(self, heartbeat: MonitorHeartbeat) -> None:
        async with self._lock:
            if self.closed:
                self._record_failure("closed", "HEARTBEAT_SINK_CLOSED")
                return

            attempted_at = datetime.now(timezone.utc)
            self._status = self._status.model_copy(
                update={"last_attempt_at": attempted_at}
            )
            try:
                payload = self._serialize(heartbeat)
            except (TypeError, ValueError):
                self._record_failure(
                    "write_failed",
                    "HEARTBEAT_WRITE_FAILED",
                    attempted_at=attempted_at,
                )
                return

            if len(payload) > MAX_HEARTBEAT_LINE_BYTES:
                self._record_failure(
                    "write_failed",
                    "HEARTBEAT_PAYLOAD_TOO_LARGE",
                    attempted_at=attempted_at,
                )
                return

            worker = asyncio.create_task(
                asyncio.to_thread(self._write_blocking, payload),
                name="tg-hub-heartbeat-write",
            )
            cancelled = False
            result: _WriteResult | None = None
            unknown_error: BaseException | None = None
            while not worker.done():
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception as exc:
                    unknown_error = exc
                    break
            if result is None and unknown_error is None:
                try:
                    result = worker.result()
                except Exception as exc:
                    unknown_error = exc

            if unknown_error is not None:
                self._record_failure(
                    "write_failed",
                    "HEARTBEAT_WRITE_FAILED",
                    attempted_at=attempted_at,
                )
            elif result is not None and result.error_code is not None:
                self._record_failure(
                    result.status,
                    result.error_code,
                    attempted_at=attempted_at,
                )
            else:
                self.last_error_code = None
                self._status = HeartbeatPersistenceStatus(
                    enabled=True,
                    last_attempt_at=attempted_at,
                    last_success_at=datetime.now(timezone.utc),
                    status="ok",
                )

            if cancelled:
                raise asyncio.CancelledError
            if unknown_error is not None:
                raise unknown_error

    async def aclose(self) -> None:
        async with self._lock:
            if self.closed:
                return
            self.closed = True
            self._status = self._status.model_copy(update={"status": "closed"})

    @staticmethod
    def _serialize(heartbeat: MonitorHeartbeat) -> bytes:
        payload = json.dumps(
            heartbeat.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return payload.encode("utf-8") + b"\n"

    def _write_blocking(self, payload: bytes) -> _WriteResult:
        lock_fd: int | None = None
        data_fd: int | None = None
        try:
            self._validate_or_create_parent()
            lock_fd = self._safe_open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | _o_nofollow(),
            )
            self._acquire_flock(lock_fd)
            data_fd = self._safe_open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY | _o_nofollow(),
            )
            self._write_all(data_fd, payload)
            return _WriteResult(status="ok")
        except _LockTimeoutError:
            return _WriteResult(
                status="lock_timeout",
                error_code="HEARTBEAT_LOCK_TIMEOUT",
            )
        except _ShortWriteError:
            return _WriteResult(
                status="short_write",
                error_code="HEARTBEAT_SHORT_WRITE",
            )
        except _PathInvalidError:
            return _WriteResult(
                status="path_invalid",
                error_code="HEARTBEAT_PATH_INVALID",
            )
        except PermissionError:
            return _WriteResult(
                status="permission_denied",
                error_code="HEARTBEAT_PERMISSION_DENIED",
            )
        except OSError:
            return _WriteResult(
                status="write_failed",
                error_code="HEARTBEAT_WRITE_FAILED",
            )
        finally:
            if data_fd is not None:
                os.close(data_fd)
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)

    def _validate_or_create_parent(self) -> None:
        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = parent.lstat()
        except PermissionError:
            raise
        except OSError as exc:
            raise _PathInvalidError from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _PathInvalidError
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise PermissionError(errno.EACCES, "runtime directory is not private")
        if not os.access(parent, os.W_OK):
            raise PermissionError(errno.EACCES, "runtime directory is not writable")

    @staticmethod
    def _safe_open(path: Path, flags: int) -> int:
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
                raise _PathInvalidError from exc
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _PathInvalidError
            os.fchmod(fd, 0o600)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _acquire_flock(self, fd: int) -> None:
        deadline = time.monotonic() + self.flock_timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise _LockTimeoutError from exc
                time.sleep(FLOCK_POLL_SECONDS)

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise _ShortWriteError
            written += count

    def _record_failure(
        self,
        status: PersistenceState,
        error_code: str,
        *,
        attempted_at: datetime | None = None,
    ) -> None:
        self.error_count += 1
        self.last_error_code = error_code
        self._status = HeartbeatPersistenceStatus(
            enabled=True,
            last_attempt_at=attempted_at or self._status.last_attempt_at,
            last_success_at=self._status.last_success_at,
            status=status,
            error_code=error_code,
        )


def create_heartbeat_sinks(
    path: str | Path,
    *,
    control_sink: HeartbeatSink | None = None,
) -> tuple[HeartbeatSink, JsonlHeartbeatSink]:
    """Build the shared CLI/Admin persistence assembly."""
    persistence = JsonlHeartbeatSink(path)
    if control_sink is None:
        return persistence, persistence
    return CompositeHeartbeatSink(control_sink, persistence), persistence


def _o_nofollow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)

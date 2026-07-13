"""Exclusive ownership for the shared Telethon session."""

from __future__ import annotations

import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import Settings

SessionOwnershipErrorCode = Literal[
    "SESSION_IN_USE",
    "SESSION_PATH_INVALID",
]


class SessionOwnershipError(RuntimeError):
    def __init__(self, error_code: SessionOwnershipErrorCode) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class SessionFileIdentity:
    device: int
    inode: int
    mode: int


def canonical_session_path(app_settings: Settings) -> Path:
    configured = app_settings.TELEGRAM_SESSION_NAME.strip()
    if not configured:
        raise SessionOwnershipError("SESSION_PATH_INVALID")
    path = Path(configured).expanduser()
    return path if path.suffix == ".session" else path.with_suffix(".session")


def validate_session_file(app_settings: Settings) -> SessionFileIdentity:
    path = canonical_session_path(app_settings)
    try:
        parent_info = path.parent.lstat()
        file_info = path.lstat()
    except OSError as exc:
        raise SessionOwnershipError("SESSION_PATH_INVALID") from exc
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_ISLNK(parent_info.st_mode)
        or parent_info.st_mode & 0o077
        or not stat.S_ISREG(file_info.st_mode)
        or stat.S_ISLNK(file_info.st_mode)
        or file_info.st_mode & 0o077
    ):
        raise SessionOwnershipError("SESSION_PATH_INVALID")
    return SessionFileIdentity(
        device=file_info.st_dev,
        inode=file_info.st_ino,
        mode=stat.S_IMODE(file_info.st_mode),
    )


class SessionOwnershipLease:
    """Own a persistent lock inode without exposing session details."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path.expanduser()
        self._fd: int | None = None

    @classmethod
    def from_settings(cls, app_settings: Settings) -> "SessionOwnershipLease":
        return cls(
            Path(app_settings.HEARTBEAT_PATH).expanduser().parent
            / "telethon-session.lock"
        )

    def acquire(self) -> None:
        if self._fd is not None:
            return
        parent = self._lock_path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent_info = parent.lstat()
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or stat.S_ISLNK(parent_info.st_mode)
                or parent_info.st_mode & 0o077
            ):
                raise SessionOwnershipError("SESSION_PATH_INVALID")
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self._lock_path, flags, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SessionOwnershipError("SESSION_PATH_INVALID")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionOwnershipError("SESSION_IN_USE") from exc
            self._fd = fd
        except SessionOwnershipError:
            if "fd" in locals():
                os.close(fd)
            raise
        except OSError as exc:
            if "fd" in locals():
                os.close(fd)
            raise SessionOwnershipError("SESSION_PATH_INVALID") from exc

    def verify_identity(
        self,
        app_settings: Settings,
        expected: SessionFileIdentity,
    ) -> None:
        if validate_session_file(app_settings) != expected:
            raise SessionOwnershipError("SESSION_PATH_INVALID")

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def __enter__(self) -> "SessionOwnershipLease":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

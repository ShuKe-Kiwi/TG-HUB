"""Low-level path, fd, locking, and atomic-write helpers for rotation."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class RotationFsError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def nofollow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def validate_directory(path: Path, *, create: bool) -> None:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except PermissionError as exc:
        raise RotationFsError("ROTATION_PERMISSION_DENIED") from exc
    except OSError as exc:
        raise RotationFsError("ROTATION_PATH_INVALID") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RotationFsError("ROTATION_PATH_INVALID")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RotationFsError("ROTATION_PERMISSION_DENIED")


def open_regular(path: Path, flags: int, *, mode: int = 0o600) -> int:
    try:
        fd = os.open(path, flags | nofollow(), mode)
    except PermissionError as exc:
        raise RotationFsError("ROTATION_PERMISSION_DENIED") from exc
    except OSError as exc:
        raise RotationFsError("ROTATION_PATH_INVALID") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RotationFsError("ROTATION_ACTIVE_NOT_REGULAR")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(fd, payload[offset:])
        except OSError as exc:
            raise RotationFsError("ROTATION_WRITE_FAILED") from exc
        if written <= 0:
            raise RotationFsError("ROTATION_WRITE_FAILED")
        offset += written


def fsync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise RotationFsError("ROTATION_FSYNC_FAILED") from exc


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RotationFsError("ROTATION_FSYNC_FAILED") from exc
    try:
        fsync_fd(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: dict, *, token: str) -> None:
    temp = path.parent / f".{path.name}.{token}.tmp"
    fd: int | None = None
    try:
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
        ):
            raise RotationFsError("ROTATION_PATH_INVALID")
        fd = open_regular(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        write_all(fd, encoded + b"\n")
        fsync_fd(fd)
        os.close(fd)
        fd = None
        os.replace(temp, path)
        fsync_directory(path.parent)
    except RotationFsError:
        raise
    except OSError as exc:
        raise RotationFsError("ROTATION_STATUS_WRITE_FAILED") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def read_json_regular(path: Path) -> dict | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RotationFsError("ROTATION_PATH_INVALID")
    fd = open_regular(path, os.O_RDONLY)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        raise RotationFsError("ROTATION_RECOVERY_REQUIRED") from exc
    if not isinstance(value, dict):
        raise RotationFsError("ROTATION_RECOVERY_REQUIRED")
    return value


@contextmanager
def exclusive_flock(path: Path) -> Iterator[int]:
    fd = open_regular(path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RotationFsError("ROTATION_ALREADY_RUNNING") from exc
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

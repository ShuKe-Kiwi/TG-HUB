"""Low-level backup-root lock contract for P6-Deploy-4A."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

BackupLockMode = Literal["shared", "exclusive"]


class BackupFsError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _nofollow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def validate_backup_root(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupFsError("BACKUP_PATH_INVALID") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BackupFsError("BACKUP_PATH_INVALID")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise BackupFsError("BACKUP_PATH_INVALID")


def _open_lock(path: Path) -> int:
    validate_backup_root(path.parent)
    try:
        fd = os.open(
            path,
            os.O_CREAT | os.O_RDWR | _nofollow(),
            0o600,
        )
    except OSError as exc:
        raise BackupFsError("BACKUP_PATH_INVALID") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BackupFsError("BACKUP_PATH_INVALID")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def backup_root_lock(root: Path, *, mode: BackupLockMode) -> Iterator[int]:
    """Acquire the shared validation or exclusive lifecycle lock without waiting."""
    if mode not in {"shared", "exclusive"}:
        raise BackupFsError("BACKUP_PATH_INVALID")
    fd = _open_lock(root / ".backup.lock")
    operation = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
    try:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupFsError("BACKUP_IN_PROGRESS") from exc
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

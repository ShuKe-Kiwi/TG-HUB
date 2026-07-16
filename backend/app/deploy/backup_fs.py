"""Low-level backup-root lock contract for P6-Deploy-4A."""

from __future__ import annotations

import fcntl
import hashlib
import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

BackupLockMode = Literal["shared", "exclusive"]
READ_CHUNK_BYTES = 1024 * 1024


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


def ensure_private_directory(path: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise BackupFsError("BACKUP_PATH_INVALID")
        else:
            path.mkdir(mode=0o700, parents=True)
        os.chmod(path, 0o700)
    except BackupFsError:
        raise
    except OSError as exc:
        raise BackupFsError("BACKUP_PATH_INVALID") from exc
    validate_backup_root(path)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _nofollow()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BackupFsError("BACKUP_PATH_INVALID") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise BackupFsError("BACKUP_PATH_INVALID") from exc
    finally:
        os.close(fd)


def open_regular(path: Path, flags: int, *, mode: int = 0o600) -> int:
    try:
        fd = os.open(path, flags | _nofollow(), mode)
    except OSError as exc:
        raise BackupFsError("BACKUP_PATH_INVALID") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BackupFsError("BACKUP_PATH_INVALID")
        if flags & os.O_ACCMODE != os.O_RDONLY:
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
            if exc.errno == errno.ENOSPC:
                raise BackupFsError("BACKUP_SPACE_INSUFFICIENT") from exc
            raise BackupFsError("BACKUP_PATH_INVALID") from exc
        if written <= 0:
            raise BackupFsError("BACKUP_PATH_INVALID")
        offset += written


def atomic_write_bytes(path: Path, payload: bytes, *, token: str) -> None:
    temp = path.parent / f".{path.name}.{token}.tmp"
    fd: int | None = None
    try:
        fd = open_regular(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temp, path)
        fsync_directory(path.parent)
    except BackupFsError:
        raise
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise BackupFsError("BACKUP_SPACE_INSUFFICIENT") from exc
        raise BackupFsError("BACKUP_PATH_INVALID") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def create_bytes_if_absent(path: Path, payload: bytes, *, token: str) -> bool:
    """Durably publish a complete regular file without replacing an existing one."""
    temp = path.parent / f".{path.name}.{token}.tmp"
    fd: int | None = None
    try:
        fd = open_regular(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            os.link(temp, path, follow_symlinks=False)
        except FileExistsError:
            return False
        fsync_directory(path.parent)
        return True
    except BackupFsError:
        raise
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise BackupFsError("BACKUP_SPACE_INSUFFICIENT") from exc
        raise BackupFsError("BACKUP_PATH_INVALID") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def read_regular_exact(path: Path, *, max_bytes: int) -> bytes:
    fd = open_regular(path, os.O_RDONLY)
    try:
        before = os.fstat(fd)
        if before.st_size > max_bytes:
            raise BackupFsError("BACKUP_MANIFEST_INVALID")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                raise BackupFsError("BACKUP_PACKAGE_CHANGED_DURING_VERIFY")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if _identity(before) != _identity(after):
            raise BackupFsError("BACKUP_PACKAGE_CHANGED_DURING_VERIFY")
        return b"".join(chunks)
    finally:
        os.close(fd)


def stream_sha256(path: Path) -> tuple[str, int]:
    fd = open_regular(path, os.O_RDONLY)
    try:
        before = os.fstat(fd)
        remaining = before.st_size
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(fd, min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                raise BackupFsError("BACKUP_PACKAGE_CHANGED_DURING_VERIFY")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if _identity(before) != _identity(after):
            raise BackupFsError("BACKUP_PACKAGE_CHANGED_DURING_VERIFY")
        return digest.hexdigest(), before.st_size
    finally:
        os.close(fd)


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size)


def _open_lock(path: Path) -> int:
    validate_backup_root(path.parent)
    try:
        return open_regular(path, os.O_CREAT | os.O_RDWR)
    except BackupFsError:
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

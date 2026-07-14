from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

import pytest

from app.deploy.backup_fs import BackupFsError, backup_root_lock


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    return root


def test_shared_locks_can_coexist_and_lock_file_is_private(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with backup_root_lock(root, mode="shared"):
        with backup_root_lock(root, mode="shared"):
            assert stat.S_IMODE((root / ".backup.lock").stat().st_mode) == 0o600


def test_exclusive_lock_rejects_shared_or_exclusive_competitor(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with backup_root_lock(root, mode="exclusive"):
        for mode in ("shared", "exclusive"):
            with pytest.raises(BackupFsError, match="BACKUP_IN_PROGRESS"):
                with backup_root_lock(root, mode=mode):
                    pass


def test_shared_lock_blocks_retention_exclusive_lock(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with backup_root_lock(root, mode="shared"):
        with pytest.raises(BackupFsError, match="BACKUP_IN_PROGRESS"):
            with backup_root_lock(root, mode="exclusive"):
                pass


def test_lock_is_released_after_context_exit(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with backup_root_lock(root, mode="exclusive"):
        pass


def test_unknown_lock_mode_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with pytest.raises(BackupFsError, match="BACKUP_PATH_INVALID"):
        with backup_root_lock(root, mode="invalid"):  # type: ignore[arg-type]
            pass
    with backup_root_lock(root, mode="exclusive"):
        pass


def test_backup_root_rejects_symlink_and_open_permissions(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = tmp_path / "backups"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BackupFsError, match="BACKUP_PATH_INVALID"):
        with backup_root_lock(link, mode="exclusive"):
            pass

    link.unlink()
    link.mkdir(mode=0o755)
    with pytest.raises(BackupFsError, match="BACKUP_PATH_INVALID"):
        with backup_root_lock(link, mode="exclusive"):
            pass


def test_lock_path_rejects_symlink(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = root / "target"
    target.write_text("", encoding="utf-8")
    (root / ".backup.lock").symlink_to(target)

    with pytest.raises(BackupFsError, match="BACKUP_PATH_INVALID"):
        with backup_root_lock(root, mode="exclusive"):
            pass


def test_external_flock_is_observed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    lock_path = root / ".backup.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BackupFsError, match="BACKUP_IN_PROGRESS"):
            with backup_root_lock(root, mode="shared"):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

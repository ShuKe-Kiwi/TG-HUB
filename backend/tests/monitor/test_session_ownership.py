from pathlib import Path

import pytest

from app.config import Settings
from app.modules.monitor.session_ownership import (
    SessionOwnershipError,
    SessionOwnershipLease,
    validate_session_file,
)


def _settings(tmp_path: Path) -> Settings:
    session = tmp_path / "account.session"
    session.write_bytes(b"")
    session.chmod(0o600)
    return Settings(
        TELEGRAM_SESSION_NAME=str(session),
        HEARTBEAT_PATH=tmp_path / "runtime" / "heartbeat.jsonl",
    )


def test_session_lease_is_non_blocking_and_keeps_lock_inode(tmp_path: Path) -> None:
    configured = _settings(tmp_path)
    identity = validate_session_file(configured)
    first = SessionOwnershipLease.from_settings(configured)
    second = SessionOwnershipLease.from_settings(configured)

    first.acquire()
    first.verify_identity(configured, identity)
    with pytest.raises(SessionOwnershipError) as caught:
        second.acquire()
    assert caught.value.error_code == "SESSION_IN_USE"

    lock_path = tmp_path / "runtime" / "telethon-session.lock"
    first.release()
    assert lock_path.exists()
    second.acquire()
    second.release()


def test_session_validation_rejects_group_readable_file(tmp_path: Path) -> None:
    configured = _settings(tmp_path)
    (tmp_path / "account.session").chmod(0o640)

    with pytest.raises(SessionOwnershipError) as caught:
        validate_session_file(configured)

    assert caught.value.error_code == "SESSION_PATH_INVALID"

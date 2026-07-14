from __future__ import annotations

import stat
from datetime import datetime, timezone

import pytest

from app.deploy.backup_models import RestoreRecoveryRecord
from app.deploy.restore_recovery import RestoreRecoveryError, RestoreRecoveryStore


BACKUP_ID = "20260714T100055.207160Z-49502732533cd7470f883488ed0a6131"


def _record() -> RestoreRecoveryRecord:
    return RestoreRecoveryRecord(
        opaque_id="a" * 32,
        generated_target_name=(
            "tg_hub_restore_verify_20260714T120000Z_0123456789abcdef"
        ),
        identity_token="b" * 64,
        created_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        backup_id=BACKUP_ID,
        phase="planned",
    )


def test_store_writes_reads_advances_and_deletes_private_record(tmp_path) -> None:
    store = RestoreRecoveryStore(tmp_path / "runtime")
    record = _record()

    store.write(record)
    path = store.root / f"{record.opaque_id}.json"
    assert store.read(record.opaque_id) == record
    assert store.read_minimal(record.opaque_id) == (record.opaque_id, BACKUP_ID)
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    advanced = store.advance(record, "create_started")
    assert store.read(record.opaque_id).phase == "create_started"
    store.delete(advanced.opaque_id)
    assert not path.exists()


def test_store_rejects_invalid_id_record_and_phase(tmp_path) -> None:
    store = RestoreRecoveryStore(tmp_path / "runtime")
    record = _record()
    store.write(record)

    with pytest.raises(RestoreRecoveryError, match="RESTORE_RECOVERY_RECORD_INVALID"):
        store.read("../unsafe")
    with pytest.raises(RestoreRecoveryError, match="RESTORE_RECOVERY_PHASE_INVALID"):
        store.advance(record, "identity_committed")

    path = store.root / f"{record.opaque_id}.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RestoreRecoveryError, match="RESTORE_RECOVERY_RECORD_INVALID"):
        store.read(record.opaque_id)

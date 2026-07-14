"""Private recovery-record persistence for isolated restore verification."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from app.deploy.backup_fs import (
    BackupFsError,
    atomic_write_bytes,
    ensure_private_directory,
    fsync_directory,
    read_regular_exact,
)
from app.deploy.backup_models import (
    OPAQUE_ID_PATTERN,
    RecoveryPhase,
    RestoreRecoveryRecord,
    advance_recovery_phase,
)

MAX_RECOVERY_RECORD_BYTES = 64 * 1024


class RestoreRecoveryError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class RestoreRecoveryStore:
    def __init__(self, runtime_root: Path) -> None:
        self.root = runtime_root.expanduser() / "restore-recovery"

    def write(self, record: RestoreRecoveryRecord) -> None:
        try:
            ensure_private_directory(self.root)
            payload = (
                json.dumps(
                    record.model_dump(mode="json"),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            atomic_write_bytes(
                self._path(record.opaque_id), payload, token=os.urandom(8).hex()
            )
        except (BackupFsError, OSError, TypeError, ValueError) as exc:
            raise RestoreRecoveryError("RESTORE_RECOVERY_WRITE_FAILED") from exc

    def advance(
        self, record: RestoreRecoveryRecord, phase: RecoveryPhase
    ) -> RestoreRecoveryRecord:
        try:
            advanced = advance_recovery_phase(record, phase)
        except ValueError as exc:
            raise RestoreRecoveryError("RESTORE_RECOVERY_PHASE_INVALID") from exc
        self.write(advanced)
        return advanced

    def read(self, opaque_id: str) -> RestoreRecoveryRecord:
        path = self._path(opaque_id)
        try:
            payload = read_regular_exact(path, max_bytes=MAX_RECOVERY_RECORD_BYTES)
            return RestoreRecoveryRecord.model_validate_json(payload)
        except (BackupFsError, ValidationError, ValueError, OSError) as exc:
            raise RestoreRecoveryError("RESTORE_RECOVERY_RECORD_INVALID") from exc

    def read_minimal(self, opaque_id: str) -> tuple[str, str]:
        record = self.read(opaque_id)
        return record.opaque_id, record.backup_id

    def delete(self, opaque_id: str) -> None:
        path = self._path(opaque_id)
        try:
            path.unlink()
            fsync_directory(self.root)
        except (BackupFsError, OSError) as exc:
            raise RestoreRecoveryError("RESTORE_RECOVERY_WRITE_FAILED") from exc

    def _path(self, opaque_id: str) -> Path:
        if not OPAQUE_ID_PATTERN.fullmatch(opaque_id):
            raise RestoreRecoveryError("RESTORE_RECOVERY_RECORD_INVALID")
        return self.root / f"{opaque_id}.json"

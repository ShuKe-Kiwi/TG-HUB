"""Temp-only durable record storage for production-recovery rehearsals."""

from __future__ import annotations

import fcntl
import json
import os
import pickle
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from app.deploy.backup_fs import (
    BackupFsError,
    atomic_write_bytes,
    create_bytes_if_absent,
    ensure_private_directory,
    open_regular,
    read_regular_exact,
)
from app.deploy.production_recovery_models import (
    ProductionRecoveryRecord,
    RecoveryPhase,
    advance_production_recovery,
)

MAX_RECORD_BYTES = 128 * 1024
_CAPABILITY_SECRET = object()
_PROCESS_NONCE = secrets.token_hex(16)


class ProductionRecoveryError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class TempRecoveryCapability:
    __slots__ = ("root", "device", "inode", "process_nonce", "purpose", "_secret")

    def __init__(self, root: Path, secret: object) -> None:
        if secret is not _CAPABILITY_SECRET:
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_NOT_AUTHORIZED")
        info = root.lstat()
        self.root = root.resolve()
        self.device = info.st_dev
        self.inode = info.st_ino
        self.process_nonce = _PROCESS_NONCE
        self.purpose = "production_recovery_rehearsal"
        self._secret = secret

    def __reduce__(self) -> object:
        raise pickle.PicklingError("TempRecoveryCapability is not serializable")


def create_temp_recovery_capability(root: Path) -> TempRecoveryCapability:
    resolved = root.expanduser().resolve(strict=False)
    temp_root = Path(tempfile.gettempdir()).resolve()
    forbidden = ((Path.home() / ".tg-hub").resolve(),)
    if (resolved != temp_root and temp_root not in resolved.parents) or any(
        resolved == item or item in resolved.parents for item in forbidden
    ):
        raise ProductionRecoveryError("PRODUCTION_RECOVERY_NOT_AUTHORIZED")
    ensure_private_directory(resolved)
    return TempRecoveryCapability(resolved, _CAPABILITY_SECRET)


class TempRecoveryRecordStore:
    def __init__(self, capability: TempRecoveryCapability) -> None:
        self.capability = capability
        self._validate_capability()
        self.root = capability.root / "production-recovery"
        ensure_private_directory(self.root)
        self.lock_path = self.root / "production-recovery.lock"
        create_bytes_if_absent(self.lock_path, b"", token=secrets.token_hex(8))
        lock_info = self.lock_path.lstat()
        self._lock_identity = (lock_info.st_dev, lock_info.st_ino)

    def _validate_capability(self) -> None:
        info = self.capability.root.lstat()
        if (
            self.capability._secret is not _CAPABILITY_SECRET
            or self.capability.process_nonce != _PROCESS_NONCE
            or self.capability.purpose != "production_recovery_rehearsal"
            or info.st_dev != self.capability.device
            or info.st_ino != self.capability.inode
            or self.capability.root.is_symlink()
        ):
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_NOT_AUTHORIZED")

    def create(self, record: ProductionRecoveryRecord) -> None:
        self._validate_capability()
        payload = self._serialize(record)
        with self._locked():
            try:
                created = create_bytes_if_absent(
                    self._path(record.incident_id),
                    payload,
                    token=os.urandom(8).hex(),
                )
                if not created:
                    raise ProductionRecoveryError(
                        "PRODUCTION_RECOVERY_RECORD_ALREADY_EXISTS"
                    )
            except (BackupFsError, OSError, ValueError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_RECORD_WRITE_FAILED"
                ) from exc

    def read(self, incident_id: str) -> ProductionRecoveryRecord:
        self._validate_capability()
        with self._locked():
            try:
                return self._read_unlocked(incident_id)
            except (BackupFsError, OSError, ValueError, ValidationError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_RECORD_INVALID"
                ) from exc

    def advance(
        self, record: ProductionRecoveryRecord, target: RecoveryPhase
    ) -> ProductionRecoveryRecord:
        self._validate_capability()
        with self._locked():
            current = self._read_unlocked(record.incident_id)
            if current != record:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_STALE")
            try:
                updated = advance_production_recovery(current, target)
            except ValueError as exc:
                raise ProductionRecoveryError(str(exc)) from exc
            try:
                atomic_write_bytes(
                    self._path(updated.incident_id),
                    self._serialize(updated),
                    token=os.urandom(8).hex(),
                )
            except (BackupFsError, OSError, ValueError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_RECORD_WRITE_FAILED"
                ) from exc
            return updated

    @staticmethod
    def _serialize(record: ProductionRecoveryRecord) -> bytes:
        return (
            json.dumps(
                record.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )

    def _read_unlocked(self, incident_id: str) -> ProductionRecoveryRecord:
        return ProductionRecoveryRecord.model_validate_json(
            read_regular_exact(self._path(incident_id), max_bytes=MAX_RECORD_BYTES)
        )

    def _path(self, incident_id: str) -> Path:
        if len(incident_id) != 32 or any(c not in "0123456789abcdef" for c in incident_id):
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_INVALID")
        return self.root / f"{incident_id}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            fd = open_regular(self.lock_path, os.O_RDWR)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != self._lock_identity
            ):
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_LOCK_INVALID"
                )
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        except ProductionRecoveryError:
            raise
        except (BackupFsError, OSError) as exc:
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_LOCK_INVALID") from exc
        finally:
            if "fd" in locals():
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


class ProductionRecoveryAdapter:
    """Permanent fail-closed boundary until a later production gate authorizes it."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ProductionRecoveryError("PRODUCTION_RECOVERY_NOT_AUTHORIZED")

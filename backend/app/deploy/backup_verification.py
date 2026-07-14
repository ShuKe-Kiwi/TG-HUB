"""Trusted restore-verification sidecars for immutable backup packages."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.deploy.backup_fs import (
    BackupFsError,
    atomic_write_bytes,
    ensure_private_directory,
    read_regular_exact,
    validate_backup_root,
)
from app.deploy.backup_models import (
    BackupManifest,
    BackupVerificationSidecar,
    validate_backup_id,
)

MAX_VERIFICATION_SIDECAR_BYTES = 64 * 1024
VerificationStatus = Literal["missing", "valid", "invalid"]


class BackupVerificationError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class VerificationObservation:
    status: VerificationStatus
    restore_verified: Literal["yes", "no"]
    verification_version: int | None = None
    error_code: str | None = None


class BackupVerificationStore:
    def __init__(self, backup_root: Path) -> None:
        self.backup_root = backup_root.expanduser()
        self.root = self.backup_root / ".verifications"

    def write_passed(
        self,
        *,
        manifest: BackupManifest,
        manifest_sha256: str,
        verified_at_utc: datetime | None = None,
    ) -> BackupVerificationSidecar:
        try:
            sidecar = BackupVerificationSidecar(
                backup_id=manifest.backup_id,
                verified_at_utc=verified_at_utc or datetime.now(timezone.utc),
                manifest_sha256=manifest_sha256,
                database_dump_sha256=manifest.database.sha256,
                watchlist_snapshot_sha256=manifest.watchlist.sha256,
            )
            validate_backup_root(self.backup_root)
            ensure_private_directory(self.root)
            payload = (
                json.dumps(
                    sidecar.model_dump(mode="json"),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            atomic_write_bytes(
                self._path(manifest.backup_id),
                payload,
                token=os.urandom(8).hex(),
            )
            return sidecar
        except (
            BackupFsError,
            OSError,
            TypeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise BackupVerificationError(
                "BACKUP_VERIFICATION_WRITE_FAILED"
            ) from exc

    def observe(
        self,
        *,
        manifest: BackupManifest,
        manifest_sha256: str,
    ) -> VerificationObservation:
        if not self.root.exists() and not self.root.is_symlink():
            return VerificationObservation("missing", "no")
        try:
            validate_backup_root(self.root)
            path = self._path(manifest.backup_id)
            if not path.exists() and not path.is_symlink():
                return VerificationObservation("missing", "no")
            payload = read_regular_exact(
                path,
                max_bytes=MAX_VERIFICATION_SIDECAR_BYTES,
            )
            sidecar = BackupVerificationSidecar.model_validate_json(payload)
        except (BackupFsError, ValidationError, ValueError, OSError):
            return VerificationObservation(
                "invalid",
                "no",
                error_code="BACKUP_VERIFICATION_IDENTITY_INVALID",
            )
        if (
            sidecar.backup_id != manifest.backup_id
            or sidecar.manifest_sha256 != manifest_sha256
            or sidecar.database_dump_sha256 != manifest.database.sha256
            or sidecar.watchlist_snapshot_sha256 != manifest.watchlist.sha256
        ):
            return VerificationObservation(
                "invalid",
                "no",
                verification_version=sidecar.verification_version,
                error_code="BACKUP_VERIFICATION_IDENTITY_INVALID",
            )
        return VerificationObservation(
            "valid",
            "yes",
            verification_version=sidecar.verification_version,
        )

    def sidecar_backup_ids(self) -> set[str]:
        if not self.root.exists() and not self.root.is_symlink():
            return set()
        try:
            validate_backup_root(self.root)
            result: set[str] = set()
            for entry in self.root.iterdir():
                if entry.suffix != ".json":
                    continue
                try:
                    result.add(validate_backup_id(entry.stem))
                except ValueError:
                    continue
            return result
        except (BackupFsError, OSError) as exc:
            raise BackupVerificationError(
                "BACKUP_VERIFICATION_IDENTITY_INVALID"
            ) from exc

    def _path(self, backup_id: str) -> Path:
        return self.root / f"{validate_backup_id(backup_id)}.json"

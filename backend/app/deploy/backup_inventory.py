"""Read-only backup inventory and sidecar status projection for P6-Deploy-4D-1."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings, load_settings
from app.deploy.backup_fs import (
    BackupFsError,
    backup_root_lock,
    read_regular_exact,
    stream_sha256,
    validate_backup_root,
)
from app.deploy.backup_models import (
    BACKUP_ID_PATTERN,
    BackupInventoryItem,
    BackupInventoryResult,
    BackupManifest,
    BackupPinSidecar,
    BackupRecoveryHoldSidecar,
)
from app.deploy.backup_service import BackupServiceError, PgToolRunner
from app.deploy.backup_verification import (
    BackupVerificationError,
    BackupVerificationStore,
)
from app.deploy.backup_verify import BackupPackageValidator, MAX_MANIFEST_BYTES

MAX_SIDECAR_BYTES = 64 * 1024
RESERVED_ROOT_ENTRIES = {
    ".backup.lock",
    ".pins",
    ".pin-audit",
    ".recovery-holds",
    ".retention-pending",
    ".tmp",
    ".verifications",
}


class BackupInventoryService:
    def __init__(
        self,
        settings: Settings,
        *,
        validator: BackupPackageValidator | None = None,
        runner: PgToolRunner | None = None,
        pg_restore_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.validator = validator or BackupPackageValidator(
            settings, runner=runner, pg_restore_path=pg_restore_path
        )
        self.verifications = BackupVerificationStore(settings.BACKUP_DIR)

    async def inventory(self) -> BackupInventoryResult:
        root = self.settings.BACKUP_DIR.expanduser()
        try:
            validate_backup_root(root)
            with backup_root_lock(root, mode="shared"):
                return await self._inventory_locked(root)
        except (BackupFsError, BackupServiceError, BackupVerificationError):
            return self._failed()

    async def target_locked(
        self, root: Path, backup_id: str
    ) -> BackupInventoryItem:
        """Project one package while the caller owns the backup-root lock."""
        if root != self.settings.BACKUP_DIR.expanduser():
            raise BackupFsError("BACKUP_PATH_INVALID")
        if not BACKUP_ID_PATTERN.fullmatch(backup_id):
            raise BackupFsError("BACKUP_INVENTORY_INVALID")
        return await self._item_locked(root, root / backup_id, backup_id)

    async def _inventory_locked(self, root: Path) -> BackupInventoryResult:
        entries: list[BackupInventoryItem] = []
        package_ids: set[str] = set()
        unrecognized = 0
        for entry in root.iterdir():
            if entry.name in RESERVED_ROOT_ENTRIES:
                continue
            if not BACKUP_ID_PATTERN.fullmatch(entry.name):
                unrecognized += 1
                continue
            package_ids.add(entry.name)
            entries.append(await self._item_locked(root, entry, entry.name))

        orphan_verifications = self.verifications.sidecar_backup_ids() - package_ids
        unrecognized += len(orphan_verifications)
        entries.sort(
            key=lambda item: (
                item.created_at_utc is not None,
                item.created_at_utc,
                item.backup_id,
            ),
            reverse=True,
        )
        manual = sum(item.retention_disposition == "manual_review" for item in entries)
        return BackupInventoryResult(
            status="pass",
            entries=tuple(entries),
            package_count=len(entries),
            valid_count=sum(item.manifest_status == "pass" for item in entries),
            restore_verified_count=sum(
                item.restore_verified == "yes" for item in entries
            ),
            pinned_count=sum(item.pinned == "yes" for item in entries),
            manual_review_count=manual + len(orphan_verifications),
            unrecognized_entry_count=unrecognized,
            total_observed_bytes=sum(item.package_bytes or 0 for item in entries),
        )

    async def _item_locked(
        self, root: Path, package: Path, backup_id: str
    ) -> BackupInventoryItem:
        observed_bytes = _direct_regular_bytes(package)
        validation = await self.validator.validate_locked(backup_id)
        if validation.status != "pass":
            return BackupInventoryItem(
                backup_id=backup_id,
                created_at_utc=None,
                package_bytes=observed_bytes,
                manifest_status="fail",
                database_dump_status="fail",
                watchlist_snapshot_status="fail",
                catalog_status="fail",
                restore_verified="no",
                verification_status="missing",
                verification_version=None,
                pinned="no",
                pin_reason_code=None,
                retention_disposition="manual_review",
                error_code=validation.error_code or "BACKUP_INVENTORY_INVALID",
            )
        try:
            manifest_payload = read_regular_exact(
                package / "manifest.json", max_bytes=MAX_MANIFEST_BYTES
            )
            manifest = BackupManifest.model_validate_json(manifest_payload)
            manifest_sha256, _ = stream_sha256(package / "manifest.json")
            verification = self.verifications.observe(
                manifest=manifest, manifest_sha256=manifest_sha256
            )
            pinned, pin_reason, pin_error = _observe_pin(root, manifest.backup_id)
            held, hold_status, hold_error = _observe_recovery_hold(
                root, manifest.backup_id, manifest_sha256
            )
        except (BackupFsError, ValidationError, ValueError, OSError):
            return BackupInventoryItem(
                backup_id=backup_id,
                created_at_utc=None,
                package_bytes=observed_bytes,
                manifest_status="fail",
                database_dump_status="fail",
                watchlist_snapshot_status="fail",
                catalog_status="fail",
                restore_verified="no",
                verification_status="invalid",
                verification_version=None,
                pinned="no",
                pin_reason_code=None,
                retention_disposition="manual_review",
                error_code="BACKUP_INVENTORY_INVALID",
            )
        error_code = verification.error_code or pin_error or hold_error
        manual_review = error_code is not None
        protected = pinned == "yes" or held == "yes"
        return BackupInventoryItem(
            backup_id=backup_id,
            created_at_utc=manifest.created_at_utc,
            package_bytes=observed_bytes,
            manifest_status="pass",
            database_dump_status="pass",
            watchlist_snapshot_status="pass",
            catalog_status="pass",
            restore_verified=verification.restore_verified,
            verification_status=verification.status,
            verification_version=verification.verification_version,
            pinned=pinned,
            pin_reason_code=pin_reason,
            recovery_held=held,
            recovery_hold_status=hold_status,
            retention_disposition=(
                "manual_review" if manual_review else "protected" if protected else "keep"
            ),
            error_code=error_code,
        )

    @staticmethod
    def _failed() -> BackupInventoryResult:
        return BackupInventoryResult(
            status="fail",
            entries=(),
            package_count=0,
            valid_count=0,
            restore_verified_count=0,
            pinned_count=0,
            manual_review_count=0,
            unrecognized_entry_count=0,
            total_observed_bytes=0,
            error_code="BACKUP_INVENTORY_INVALID",
        )


def _direct_regular_bytes(package: Path) -> int:
    try:
        info = package.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return 0
        total = 0
        for entry in package.iterdir():
            item = entry.lstat()
            if stat.S_ISREG(item.st_mode) and not stat.S_ISLNK(item.st_mode):
                total += item.st_size
        return total
    except OSError:
        return 0


def _observe_pin(
    root: Path, backup_id: str
) -> tuple[str, str | None, str | None]:
    pin_root = root / ".pins"
    if not pin_root.exists() and not pin_root.is_symlink():
        return "no", None, None
    path = pin_root / f"{backup_id}.json"
    if not path.exists() and not path.is_symlink():
        return "no", None, None
    try:
        validate_backup_root(pin_root)
        pin = BackupPinSidecar.model_validate_json(
            read_regular_exact(path, max_bytes=MAX_SIDECAR_BYTES)
        )
        if pin.backup_id != backup_id:
            raise ValueError("pin identity mismatch")
        return "yes", pin.reason_code, None
    except (BackupFsError, ValidationError, ValueError, OSError):
        return "no", None, "BACKUP_PIN_INVALID"


def _observe_recovery_hold(
    root: Path, backup_id: str, package_identity: str
) -> tuple[str, str, str | None]:
    hold_root = root / ".recovery-holds"
    if not hold_root.exists() and not hold_root.is_symlink():
        return "no", "missing", None
    try:
        validate_backup_root(hold_root)
        matches = list(hold_root.glob(f"{backup_id}.*.json"))
        if not matches:
            return "no", "missing", None
        if len(matches) != 1:
            return "no", "invalid", "BACKUP_INVENTORY_INVALID"
        hold = BackupRecoveryHoldSidecar.model_validate_json(
            read_regular_exact(matches[0], max_bytes=MAX_SIDECAR_BYTES)
        )
        if (
            hold.selected_backup_id != backup_id
            or hold.package_identity != package_identity
            or matches[0].name != f"{backup_id}.{hold.incident_id}.json"
        ):
            raise ValueError("hold identity mismatch")
        incident = (
            root.parent
            / "runtime"
            / "production-recovery"
            / f"{hold.incident_id}.json"
        )
        if not incident.is_file():
            return "yes", "orphaned", "BACKUP_RECOVERY_HOLD_ORPHANED"
        return "yes", "valid", None
    except (BackupFsError, ValidationError, ValueError, OSError):
        return "no", "invalid", "BACKUP_INVENTORY_INVALID"


async def _main() -> int:
    if len(sys.argv) != 1:
        print("usage: python -m app.deploy.backup_inventory")
        return 2
    result = await BackupInventoryService(load_settings()).inventory()
    print(result.model_dump_json())
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.deploy import backup_retention
from app.deploy.backup_inventory import BackupInventoryService
from app.deploy.backup_models import (
    BackupExclusions,
    BackupManifest,
    DatabaseBackupManifest,
    WatchlistBackupManifest,
)
from app.deploy.backup_retention import (
    PinMutationService,
    RetentionPlanService,
    RetentionPolicy,
    TempRetentionApplyService,
    create_temp_mutation_capability,
)
from app.deploy.backup_service import ToolResult
from app.deploy.backup_verification import BackupVerificationStore
from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.watchlist_service import watchlist_revision

UTC = timezone.utc
REFERENCE = datetime(2026, 7, 16, 12, tzinfo=UTC)


class FakeRunner:
    async def run(self, argv, *, env, cwd=None):
        values = [str(value) for value in argv]
        if "--version" in values:
            return ToolResult(0, b"pg_restore (PostgreSQL) 16.4\n", b"")
        tables = [
            "alembic_version", "channels", "raw_messages", "works",
            "resources", "resource_links", "resource_sources",
        ]
        lines = []
        for index, table in enumerate(tables, 1):
            lines.append(f"{index}; 1259 1 TABLE public {table} owner")
            if table != "alembic_version":
                lines.append(f"{index + 20}; 0 1 TABLE DATA public {table} owner")
        return ToolResult(0, ("\n".join(lines) + "\n").encode(), b"")


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700, parents=True)
    return Settings(BACKUP_DIR=root)


def _backup_id(created: datetime, suffix: int) -> str:
    return f"{created:%Y%m%dT%H%M%S}.000000Z-{suffix:032x}"


def _package(
    settings: Settings,
    created: datetime,
    suffix: int,
    *,
    verified: bool = False,
) -> str:
    backup_id = _backup_id(created, suffix)
    package = settings.BACKUP_DIR / backup_id
    package.mkdir(mode=0o700)
    dump = f"dump-{suffix}".encode()
    watch = json.dumps(
        {
            "source_channels": [{"ref": "https://t.me/example", "enabled": True}],
            "watch_titles": [{"title": "百花杀", "enabled": True, "aliases": []}],
        },
        ensure_ascii=False,
    ).encode()
    manifest = BackupManifest(
        backup_id=backup_id,
        created_at_utc=created,
        app_git_commit="a" * 40,
        git_worktree_clean=True,
        python_version="3.11",
        dependency_lock_sha256="b" * 64,
        alembic_revision="head",
        config_schema_version=1,
        database=DatabaseBackupManifest(
            sha256=hashlib.sha256(dump).hexdigest(),
            size_bytes=len(dump),
            source_server_version="16.4",
            source_server_major=16,
            pg_dump_version="pg_dump (PostgreSQL) 16.4",
            pg_dump_major=16,
        ),
        watchlist=WatchlistBackupManifest(
            sha256=hashlib.sha256(watch).hexdigest(),
            size_bytes=len(watch),
            revision=watchlist_revision(WatchlistConfig.model_validate_json(watch)),
        ),
        exclusions=BackupExclusions(
            production_env="secret_material_excluded",
            telethon_session="authentication_session_excluded",
            logs="operational_data_excluded",
            runtime_state="ephemeral_data_excluded",
        ),
    )
    (package / "database.dump").write_bytes(dump)
    (package / "watchlist.json").write_bytes(watch)
    manifest_path = package / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    for path in package.iterdir():
        path.chmod(0o600)
    if verified:
        BackupVerificationStore(settings.BACKUP_DIR).write_passed(
            manifest=manifest,
            manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )
    return backup_id


def _service(settings: Settings, *, policy: RetentionPolicy) -> RetentionPlanService:
    inventory = BackupInventoryService(
        settings, runner=FakeRunner(), pg_restore_path=Path("/tools/pg_restore")
    )
    return RetentionPlanService(
        settings, inventory=inventory, policy=policy, clock=lambda: REFERENCE
    )


async def test_plan_uses_frozen_clock_and_protects_verified_floor(tmp_path) -> None:
    settings = _settings(tmp_path)
    oldest = _package(settings, REFERENCE - timedelta(days=20), 1)
    middle = _package(settings, REFERENCE - timedelta(days=10), 2)
    newest = _package(settings, REFERENCE, 3, verified=True)
    service = _service(
        settings,
        policy=RetentionPolicy(daily_slots=1, weekly_slots=0, minimum_valid_packages=1),
    )

    result = await service.plan()

    assert result.status == "pass"
    assert result.plan.selection_reference_at_utc == REFERENCE
    assert result.plan.verified_floor_backup_id == newest
    assert result.plan.protected_backup_ids == (newest,)
    assert result.plan.candidate_backup_ids == (oldest, middle)


async def test_single_verified_package_has_no_candidate(tmp_path) -> None:
    settings = _settings(tmp_path)
    backup_id = _package(settings, REFERENCE, 1, verified=True)

    result = await _service(settings, policy=RetentionPolicy()).plan()

    assert result.status == "pass"
    assert result.plan.protected_backup_ids == (backup_id,)
    assert result.plan.candidate_backup_ids == ()


async def test_plan_fails_closed_for_unrecognized_entry(tmp_path) -> None:
    settings = _settings(tmp_path)
    _package(settings, REFERENCE, 1, verified=True)
    (settings.BACKUP_DIR / "unexpected").write_text("x", encoding="utf-8")

    result = await _service(settings, policy=RetentionPolicy()).plan()

    assert result.status == "fail"
    assert result.error_code == "BACKUP_RETENTION_INVENTORY_UNSAFE"
    assert not (settings.BACKUP_DIR.parent / "runtime" / "retention-plans").exists()


async def test_unrecoverable_budget_returns_stable_failure_with_plan(tmp_path) -> None:
    settings = _settings(tmp_path)
    oldest = _package(settings, REFERENCE - timedelta(days=20), 1)
    newest = _package(settings, REFERENCE, 2, verified=True)

    result = await _service(
        settings,
        policy=RetentionPolicy(
            daily_slots=1, weekly_slots=0, minimum_valid_packages=1,
            archive_budget_bytes=1,
        ),
    ).plan()

    assert result.status == "fail"
    assert result.error_code == "BACKUP_BUDGET_EXCEEDED_UNRECOVERABLE"
    assert result.plan.candidate_backup_ids == (oldest,)
    assert result.plan.protected_backup_ids == (newest,)


def test_pin_is_idempotent_conflict_safe_and_audited(tmp_path) -> None:
    settings = _settings(tmp_path)
    backup_id = _package(settings, REFERENCE, 1, verified=True)
    capability = create_temp_mutation_capability(settings.BACKUP_DIR)
    service = PinMutationService(settings, capability, clock=lambda: REFERENCE)

    created = service.pin(backup_id, "pre_upgrade")
    pin_path = settings.BACKUP_DIR / ".pins" / f"{backup_id}.json"
    first_mtime = pin_path.stat().st_mtime_ns
    idempotent = service.pin(backup_id, "pre_upgrade")
    idempotent_mtime = pin_path.stat().st_mtime_ns
    conflict = service.pin(backup_id, "incident")
    removed = service.unpin(backup_id)
    missing = service.unpin(backup_id)

    assert created.disposition == "created"
    assert idempotent.disposition == "idempotent"
    assert idempotent_mtime == first_mtime
    assert pin_path.exists() is False
    assert conflict.error_code == "BACKUP_PIN_REASON_CONFLICT"
    assert removed.disposition == "removed"
    assert missing.disposition == "idempotent"
    journals = list((settings.BACKUP_DIR / ".pin-audit").glob("*.json"))
    assert len(journals) == 5
    assert all('"phase":"completed"' in path.read_text() for path in journals)
    assert any('"result":"rejected"' in path.read_text() for path in journals)


def test_capability_is_bound_to_exact_temp_root(tmp_path) -> None:
    first = _settings(tmp_path / "first")
    second = _settings(tmp_path / "second")
    backup_id = _package(second, REFERENCE, 1, verified=True)
    capability = create_temp_mutation_capability(first.BACKUP_DIR)

    result = PinMutationService(second, capability).pin(backup_id, "incident")

    assert result.status == "fail"
    assert result.error_code == "BACKUP_PIN_WRITE_NOT_AUTHORIZED"
    assert not (second.BACKUP_DIR / ".pins").exists()


def test_pin_reconcile_completes_mutation_started_after_commit_gap(
    tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    backup_id = _package(settings, REFERENCE, 1, verified=True)
    capability = create_temp_mutation_capability(settings.BACKUP_DIR)
    service = PinMutationService(settings, capability, clock=lambda: REFERENCE)
    real_write = backup_retention._write_journal
    failed = False

    def fail_after_sidecar(path, journal, **changes):
        nonlocal failed
        if changes.get("phase") == "mutation_committed" and not failed:
            failed = True
            raise backup_retention.RetentionError("BACKUP_PIN_AUDIT_WRITE_FAILED")
        return real_write(path, journal, **changes)

    monkeypatch.setattr(backup_retention, "_write_journal", fail_after_sidecar)
    result = service.pin(backup_id, "incident")
    operation_id = next((settings.BACKUP_DIR / ".pin-audit").glob("*.json")).stem
    monkeypatch.setattr(backup_retention, "_write_journal", real_write)

    reconciled = service.reconcile(operation_id)

    assert result.status == "fail"
    assert result.error_code == "BACKUP_PIN_AUDIT_WRITE_FAILED"
    assert result.disposition == "unknown"
    assert reconciled.status == "pass"
    assert reconciled.disposition == "created"
    assert '"phase":"completed"' in (
        settings.BACKUP_DIR / ".pin-audit" / f"{operation_id}.json"
    ).read_text()


def test_invalid_pin_id_is_not_echoed_or_written(tmp_path) -> None:
    settings = _settings(tmp_path)
    capability = create_temp_mutation_capability(settings.BACKUP_DIR)

    result = PinMutationService(settings, capability).pin("../../secret", "incident")

    assert result.status == "fail"
    assert result.backup_id is None
    assert result.error_code == "BACKUP_PIN_INVALID"
    assert not (settings.BACKUP_DIR / ".pins").exists()


async def test_temp_apply_deletes_only_plan_candidates(tmp_path) -> None:
    settings = _settings(tmp_path)
    oldest = _package(settings, REFERENCE - timedelta(days=20), 1)
    middle = _package(settings, REFERENCE - timedelta(days=10), 2)
    newest = _package(settings, REFERENCE, 3, verified=True)
    plan_result = await _service(
        settings,
        policy=RetentionPolicy(daily_slots=1, weekly_slots=0, minimum_valid_packages=1),
    ).plan()
    capability = create_temp_mutation_capability(settings.BACKUP_DIR)

    result = TempRetentionApplyService(settings, capability).apply(plan_result.plan)

    assert result.status == "pass"
    assert result.deleted_backup_ids == (oldest, middle)
    assert (settings.BACKUP_DIR / newest).is_dir()
    assert not (settings.BACKUP_DIR / oldest).exists()
    assert not (settings.BACKUP_DIR / middle).exists()
    assert list((settings.BACKUP_DIR / ".retention-pending").iterdir()) == []


async def test_temp_apply_resumes_after_file_unlink_failure(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    oldest = _package(settings, REFERENCE - timedelta(days=20), 1)
    _package(settings, REFERENCE, 2, verified=True)
    plan = (
        await _service(
            settings,
            policy=RetentionPolicy(daily_slots=1, weekly_slots=0, minimum_valid_packages=1),
        ).plan()
    ).plan
    capability = create_temp_mutation_capability(settings.BACKUP_DIR)
    service = TempRetentionApplyService(settings, capability)
    real_unlink = backup_retention.os.unlink
    calls = 0

    def fail_second(path, *, dir_fd=None) -> None:
        nonlocal calls
        if dir_fd is not None:
            calls += 1
            if calls == 2:
                raise OSError("injected unlink failure")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(backup_retention.os, "unlink", fail_second)
    first = service.apply(plan)
    monkeypatch.setattr(backup_retention.os, "unlink", real_unlink)
    second = service.apply(plan)

    assert first.status == "fail"
    assert second.status == "pass"
    assert second.deleted_backup_ids == (oldest,)
    assert not (settings.BACKUP_DIR / oldest).exists()


async def test_temp_apply_rejects_orphan_sidecar_added_after_plan(tmp_path) -> None:
    settings = _settings(tmp_path)
    _package(settings, REFERENCE - timedelta(days=20), 1)
    _package(settings, REFERENCE, 2, verified=True)
    plan = (
        await _service(
            settings,
            policy=RetentionPolicy(daily_slots=1, weekly_slots=0, minimum_valid_packages=1),
        ).plan()
    ).plan
    orphan_root = settings.BACKUP_DIR / ".pins"
    orphan_root.mkdir(mode=0o700)
    orphan = orphan_root / f"{_backup_id(REFERENCE, 99)}.json"
    orphan.write_text("{}", encoding="utf-8")
    orphan.chmod(0o600)
    capability = create_temp_mutation_capability(settings.BACKUP_DIR)

    result = TempRetentionApplyService(settings, capability).apply(plan)

    assert result.status == "fail"
    assert result.error_code == "BACKUP_RETENTION_PLAN_STALE"

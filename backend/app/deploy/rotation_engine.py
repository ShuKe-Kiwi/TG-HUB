"""Bounded local rotation engine for fixed tg-hub observability files."""

from __future__ import annotations

import gzip
import os
import secrets
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from app.config import Settings
from app.deploy.rotation_fs import (
    RotationFsError,
    atomic_write_json,
    exclusive_flock,
    fsync_directory,
    fsync_fd,
    open_regular,
    read_json_regular,
    validate_directory,
    write_all,
)
from app.deploy.rotation_models import (
    PendingMetadata,
    RotationFileStatus,
    RotationRunResult,
    RotationStatus,
    TargetKey,
)

DEFAULT_THRESHOLD_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_AGE = timedelta(hours=24)
DEFAULT_ARCHIVE_BUDGET_BYTES = 250 * 1024 * 1024
DEFAULT_RETENTION: dict[TargetKey, int] = {
    "app.stdout.log": 7,
    "app.stderr.log": 7,
    "heartbeat.jsonl": 3,
}
_TARGETS: tuple[TargetKey, ...] = (
    "app.stdout.log",
    "app.stderr.log",
    "heartbeat.jsonl",
)


class RotationEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
        max_age: timedelta = DEFAULT_MAX_AGE,
        archive_budget_bytes: int = DEFAULT_ARCHIVE_BUDGET_BYTES,
        retention: dict[TargetKey, int] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.threshold_bytes = threshold_bytes
        self.max_age = max_age
        self.archive_budget_bytes = archive_budget_bytes
        self.retention = retention or DEFAULT_RETENTION
        self._now = now or (lambda: datetime.now(UTC))
        self.log_dir = settings.LOG_DIR.expanduser()
        self.heartbeat_path = settings.HEARTBEAT_PATH.expanduser()
        self.runtime_dir = self.heartbeat_path.parent
        self.archive_dir = self.log_dir / "archive"
        self.rotate_lock_path = self.runtime_dir / "rotate.lock"
        self.heartbeat_lock_path = self.runtime_dir / "heartbeat.lock"
        self.status_path = self.runtime_dir / "rotation-status.json"

    def run(self, *, dry_run: bool = False) -> RotationRunResult:
        run_id = secrets.token_hex(16)
        started = self._now()
        try:
            self._validate_roots(create=not dry_run)
            with exclusive_flock(self.rotate_lock_path):
                previous = self._load_previous_status()
                if not dry_run:
                    self._cleanup_disposable_temps()
                    recovered = self._recover_pending()
                else:
                    recovered = {}
                file_results = self._check_targets(
                    previous,
                    run_id=run_id,
                    dry_run=dry_run,
                    recovered=recovered,
                )
                cleaned, budget_status = self._apply_retention_and_budget(
                    dry_run=dry_run
                )
                status = self._build_status(
                    run_id=run_id,
                    started=started,
                    files=file_results,
                    cleaned=cleaned,
                    budget_status=budget_status,
                )
                if not dry_run:
                    try:
                        atomic_write_json(
                            self.status_path,
                            status.model_dump(mode="json"),
                            token=run_id,
                        )
                    except RotationFsError as exc:
                        return RotationRunResult(
                            status="fail",
                            error_code="ROTATION_STATUS_WRITE_FAILED",
                            rotated_files=status.rotated_files,
                            cleaned_archives=status.cleaned_archives,
                            archive_budget_status=status.archive_budget_status,
                            active_oversize=status.active_oversize,
                        )
                return RotationRunResult(
                    status=status.status,
                    error_code=status.error_code,
                    rotated_files=status.rotated_files,
                    would_rotate_files=sum(
                        item.trigger_reason in {"size", "age", "recovery"}
                        for item in file_results.values()
                    ),
                    cleaned_archives=status.cleaned_archives,
                    archive_budget_status=status.archive_budget_status,
                    active_oversize=status.active_oversize,
                )
        except RotationFsError as exc:
            return RotationRunResult(status="fail", error_code=exc.error_code)
        except Exception:
            return RotationRunResult(
                status="fail",
                error_code="ROTATION_UNEXPECTED_ERROR",
            )

    def _validate_roots(self, *, create: bool) -> None:
        validate_directory(self.runtime_dir, create=False)
        validate_directory(self.log_dir, create=False)
        if create or self.archive_dir.exists() or self.archive_dir.is_symlink():
            validate_directory(self.archive_dir, create=create)
        if self.settings.APP_ENV == "production":
            private_root = (Path.home() / ".tg-hub").resolve()
            for path in (
                self.runtime_dir,
                self.log_dir,
                self.archive_dir,
                self.heartbeat_path,
            ):
                try:
                    path.resolve().relative_to(private_root)
                except (OSError, ValueError) as exc:
                    raise RotationFsError("ROTATION_PATH_INVALID") from exc

    def _load_previous_status(self) -> RotationStatus | None:
        raw = read_json_regular(self.status_path)
        if raw is None:
            return None
        try:
            return RotationStatus.model_validate(raw)
        except ValidationError as exc:
            raise RotationFsError("ROTATION_RECOVERY_REQUIRED") from exc

    def _active_path(self, target: TargetKey) -> Path:
        if target == "heartbeat.jsonl":
            return self.heartbeat_path
        return self.log_dir / target

    def _check_targets(
        self,
        previous: RotationStatus | None,
        *,
        run_id: str,
        dry_run: bool,
        recovered: dict[TargetKey, str],
    ) -> dict[TargetKey, RotationFileStatus]:
        results: dict[TargetKey, RotationFileStatus] = {}
        now = self._now()
        for target in _TARGETS:
            if target in recovered:
                results[target] = RotationFileStatus(
                    last_checked_at=now,
                    active_generation_started_at=now,
                    last_rotated_at=now,
                    trigger_reason="recovery",
                    result="recovered",
                    archive_name=recovered[target],
                    legacy_content_possible=target != "heartbeat.jsonl",
                )
                continue
            old = previous.files.get(target) if previous is not None else None
            active = self._active_path(target)
            try:
                info = active.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise RotationFsError("ROTATION_ACTIVE_NOT_REGULAR")
                size = info.st_size
                generation = (
                    old.active_generation_started_at
                    if old is not None and old.active_generation_started_at
                    else datetime.fromtimestamp(
                        getattr(info, "st_birthtime", info.st_ctime), UTC
                    )
                )
                reason = "none"
                if size >= self.threshold_bytes:
                    reason = "size"
                elif size > 0 and now - generation >= self.max_age:
                    reason = "age"
                if reason == "none" or dry_run:
                    results[target] = RotationFileStatus(
                        last_checked_at=None if dry_run else now,
                        active_generation_started_at=generation,
                        last_rotated_at=old.last_rotated_at if old else None,
                        last_size_bytes=size,
                        trigger_reason=reason,
                        result="not_modified",
                        legacy_content_possible=target != "heartbeat.jsonl",
                    )
                    continue
                if target == "heartbeat.jsonl":
                    result = self._rotate_heartbeat(target, active, run_id, reason)
                else:
                    result = self._rotate_application(target, active, run_id, reason)
                results[target] = result
            except RotationFsError as exc:
                results[target] = RotationFileStatus(
                    last_checked_at=now,
                    active_generation_started_at=(
                        old.active_generation_started_at if old else None
                    ),
                    last_rotated_at=old.last_rotated_at if old else None,
                    trigger_reason="none",
                    result="failed",
                    error_code=exc.error_code,
                    legacy_content_possible=target != "heartbeat.jsonl",
                )
            except FileNotFoundError:
                results[target] = RotationFileStatus(
                    last_checked_at=None if dry_run else now,
                    active_generation_started_at=now,
                    result="not_modified",
                    legacy_content_possible=target != "heartbeat.jsonl",
                )
        return results

    def _archive_paths(self, target: TargetKey, run_id: str) -> tuple[Path, Path, Path, Path]:
        stamp = self._now().strftime("%Y%m%dT%H%M%S.%fZ")
        base = self.archive_dir / f"{target}.{stamp}.{run_id}.jsonl"
        return (
            base.with_suffix(base.suffix + ".tmp"),
            base.with_suffix(base.suffix + ".pending"),
            base.with_suffix(base.suffix + ".pending.meta"),
            base.with_suffix(base.suffix + ".gz"),
        )

    def _rotate_application(
        self,
        target: TargetKey,
        active: Path,
        run_id: str,
        reason: str,
    ) -> RotationFileStatus:
        temp, pending, meta_path, final = self._archive_paths(target, run_id)
        active_fd = open_regular(active, os.O_RDWR)
        copied = complete = truncated = 0
        try:
            initial = os.fstat(active_fd)
            copied, complete = self._copy_fixed_snapshot(active_fd, temp, initial.st_size)
            os.replace(temp, pending)
            fsync_directory(self.archive_dir)
            meta = PendingMetadata(
                target=target,
                run_id=run_id,
                phase="snapshot_committed",
                active_inode=initial.st_ino,
                snapshot_size=initial.st_size,
                complete_line_bytes=complete,
                created_at=self._now(),
            )
            atomic_write_json(
                meta_path,
                meta.model_dump(mode="json"),
                token=run_id,
            )
            before_truncate = os.fstat(active_fd)
            if before_truncate.st_ino != initial.st_ino:
                raise RotationFsError("ROTATION_TRUNCATE_FAILED")
            truncated = before_truncate.st_size
            try:
                os.ftruncate(active_fd, 0)
            except OSError as exc:
                raise RotationFsError("ROTATION_TRUNCATE_FAILED") from exc
            fsync_fd(active_fd)
            active_meta = meta.model_copy(update={"phase": "active_truncated"})
            atomic_write_json(
                meta_path,
                active_meta.model_dump(mode="json"),
                token=run_id + "a",
            )
            self._gzip_atomic(pending, final, run_id)
            pending.unlink()
            meta_path.unlink()
            fsync_directory(self.archive_dir)
        finally:
            os.close(active_fd)
            temp.unlink(missing_ok=True)
        now = self._now()
        return RotationFileStatus(
            last_checked_at=now,
            active_generation_started_at=now,
            last_rotated_at=now,
            last_size_bytes=0,
            trigger_reason=reason,
            result="rotated",
            archive_name=final.name,
            copied_bytes=copied,
            complete_line_bytes=complete,
            truncated_bytes=truncated,
            partial_tail_detected=complete < copied,
            discarded_partial_tail_bytes=copied - complete,
            legacy_content_possible=True,
        )

    def _rotate_heartbeat(
        self,
        target: TargetKey,
        active: Path,
        run_id: str,
        reason: str,
    ) -> RotationFileStatus:
        derived, pending, _, final = self._archive_paths(target, run_id)
        source_bytes = complete = 0
        with exclusive_flock(self.heartbeat_lock_path):
            info = active.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise RotationFsError("ROTATION_ACTIVE_NOT_REGULAR")
            try:
                os.rename(active, pending)
                new_fd = open_regular(
                    active,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                try:
                    fsync_fd(new_fd)
                finally:
                    os.close(new_fd)
                fsync_directory(active.parent)
            except Exception as exc:
                if pending.exists() and not active.exists():
                    try:
                        os.rename(pending, active)
                        fsync_directory(active.parent)
                    except OSError as rollback_exc:
                        raise RotationFsError(
                            "ROTATION_HEARTBEAT_RECOVERY_REQUIRED"
                        ) from rollback_exc
                if isinstance(exc, RotationFsError):
                    raise
                raise RotationFsError("ROTATION_WRITE_FAILED") from exc
        source_fd = open_regular(pending, os.O_RDONLY)
        try:
            source_bytes = os.fstat(source_fd).st_size
            _, complete = self._copy_fixed_snapshot(source_fd, derived, source_bytes)
        finally:
            os.close(source_fd)
        self._gzip_atomic(derived, final, run_id)
        pending.unlink()
        derived.unlink(missing_ok=True)
        fsync_directory(self.archive_dir)
        now = self._now()
        return RotationFileStatus(
            last_checked_at=now,
            active_generation_started_at=now,
            last_rotated_at=now,
            last_size_bytes=0,
            trigger_reason=reason,
            result="rotated",
            archive_name=final.name,
            copied_bytes=source_bytes,
            complete_line_bytes=complete,
            partial_tail_detected=complete < source_bytes,
            discarded_partial_tail_bytes=source_bytes - complete,
        )

    def _copy_fixed_snapshot(self, source_fd: int, temp: Path, size: int) -> tuple[int, int]:
        temp_fd = open_regular(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        copied = 0
        last_newline = 0
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            while copied < size:
                try:
                    chunk = os.read(source_fd, min(1024 * 1024, size - copied))
                except OSError as exc:
                    raise RotationFsError("ROTATION_READ_FAILED") from exc
                if not chunk:
                    raise RotationFsError("ROTATION_READ_FAILED")
                write_all(temp_fd, chunk)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    last_newline = copied + newline + 1
                copied += len(chunk)
            try:
                os.ftruncate(temp_fd, last_newline)
            except OSError as exc:
                raise RotationFsError("ROTATION_WRITE_FAILED") from exc
            fsync_fd(temp_fd)
        finally:
            os.close(temp_fd)
        return copied, last_newline

    def _gzip_atomic(self, source: Path, final: Path, token: str) -> None:
        temp_gz = final.with_name(f"{final.name}.{token}.tmp.gz")
        source_fd = open_regular(source, os.O_RDONLY)
        target_fd = open_regular(temp_gz, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            with os.fdopen(source_fd, "rb") as source_handle:
                source_fd = -1
                with os.fdopen(target_fd, "wb") as raw_target:
                    target_fd = -1
                    with gzip.GzipFile(
                        fileobj=raw_target,
                        mode="wb",
                        filename="",
                        mtime=0,
                    ) as compressed:
                        while True:
                            chunk = source_handle.read(1024 * 1024)
                            if not chunk:
                                break
                            compressed.write(chunk)
                    raw_target.flush()
                    fsync_fd(raw_target.fileno())
            os.replace(temp_gz, final)
            fsync_directory(self.archive_dir)
        except RotationFsError:
            raise
        except (OSError, EOFError) as exc:
            raise RotationFsError("ROTATION_COMPRESS_FAILED") from exc
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if target_fd >= 0:
                os.close(target_fd)
            temp_gz.unlink(missing_ok=True)

    def _cleanup_disposable_temps(self) -> None:
        for path in self.archive_dir.iterdir():
            if path.name.endswith(".tmp") or path.name.endswith(".tmp.gz"):
                path.unlink()

    def _recover_pending(self) -> dict[TargetKey, str]:
        recovered: dict[TargetKey, str] = {}
        for pending in sorted(self.archive_dir.glob("*.pending")):
            target = self._target_from_archive_name(pending.name)
            if target is None:
                raise RotationFsError("ROTATION_RECOVERY_REQUIRED")
            final = pending.with_suffix(".gz")
            token = self._run_id_from_archive_name(pending.name)
            if token is None:
                raise RotationFsError("ROTATION_RECOVERY_REQUIRED")
            if target == "heartbeat.jsonl":
                derived = pending.with_suffix(".tmp")
                source_fd = open_regular(pending, os.O_RDONLY)
                try:
                    self._copy_fixed_snapshot(
                        source_fd,
                        derived,
                        os.fstat(source_fd).st_size,
                    )
                finally:
                    os.close(source_fd)
                self._gzip_atomic(derived, final, token)
                derived.unlink(missing_ok=True)
                pending.unlink()
            else:
                meta_path = pending.with_suffix(".pending.meta")
                raw_meta = read_json_regular(meta_path)
                if raw_meta is None:
                    raise RotationFsError("ROTATION_RECOVERY_REQUIRED")
                try:
                    meta = PendingMetadata.model_validate(raw_meta)
                except ValidationError as exc:
                    raise RotationFsError("ROTATION_RECOVERY_REQUIRED") from exc
                if (
                    meta.target != target
                    or meta.run_id != token
                    or meta.phase != "active_truncated"
                ):
                    raise RotationFsError("ROTATION_RECOVERY_REQUIRED")
                self._gzip_atomic(pending, final, token)
                pending.unlink()
                meta_path.unlink()
            fsync_directory(self.archive_dir)
            recovered[target] = final.name
        return recovered

    @staticmethod
    def _target_from_archive_name(name: str) -> TargetKey | None:
        for target in _TARGETS:
            if name.startswith(target + ".") and name.endswith(".jsonl.pending"):
                return target
        return None

    @staticmethod
    def _run_id_from_archive_name(name: str) -> str | None:
        suffix = ".jsonl.pending"
        if not name.endswith(suffix):
            return None
        stem = name[: -len(suffix)]
        if "." not in stem:
            return None
        token = stem.rsplit(".", 1)[-1]
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            return None
        return token

    def _final_archives(self, target: TargetKey | None = None) -> list[Path]:
        paths = []
        for path in self.archive_dir.glob("*.jsonl.gz"):
            parsed = self._target_from_archive_name(path.name.removesuffix(".gz") + ".pending")
            if parsed is not None and (target is None or parsed == target):
                paths.append(path)
        return sorted(paths, key=lambda path: path.stat().st_mtime)

    def _apply_retention_and_budget(self, *, dry_run: bool) -> tuple[int, str]:
        cleaned = 0
        retained: set[Path] = set(self._final_archives())
        for target, keep in self.retention.items():
            archives = self._final_archives(target)
            for path in archives[:-keep] if keep else archives:
                if not dry_run:
                    path.unlink()
                retained.discard(path)
                cleaned += 1
        archives = sorted(retained, key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in archives)
        budget_status = "cleaned" if cleaned else "within_budget"
        for path in archives:
            if total <= self.archive_budget_bytes:
                break
            size = path.stat().st_size
            if not dry_run:
                path.unlink()
            retained.discard(path)
            total -= size
            cleaned += 1
            budget_status = "cleaned"
        if total > self.archive_budget_bytes:
            budget_status = "exceeded_unrecoverable"
        return cleaned, budget_status

    def _build_status(
        self,
        *,
        run_id: str,
        started: datetime,
        files: dict[TargetKey, RotationFileStatus],
        cleaned: int,
        budget_status: str,
    ) -> RotationStatus:
        failures = [item for item in files.values() if item.result == "failed"]
        successes = [item for item in files.values() if item.result != "failed"]
        status = "pass" if not failures else "partial" if successes else "fail"
        error_code = failures[0].error_code if failures else None
        finals = self._final_archives()
        archive_bytes = sum(path.stat().st_size for path in finals)
        active_bytes = sum(
            path.stat().st_size
            for path in (self._active_path(target) for target in _TARGETS)
            if path.exists()
        )
        observed = archive_bytes + active_bytes
        observed += sum(
            path.stat().st_size
            for path in (
                self.archive_dir.iterdir()
                if self.archive_dir.exists()
                else ()
            )
            if path.is_file() and path not in finals
        )
        return RotationStatus(
            run_id=run_id,
            check_started_at=started,
            check_completed_at=self._now(),
            status=status,
            error_code=error_code,
            rotated_files=sum(
                item.result in {"rotated", "recovered"} for item in files.values()
            ),
            cleaned_archives=cleaned,
            archive_bytes=archive_bytes,
            active_bytes=active_bytes,
            total_observed_bytes=observed,
            archive_budget_status=budget_status,
            active_oversize=any(
                path.exists() and path.stat().st_size >= self.threshold_bytes
                for path in (self._active_path(target) for target in _TARGETS)
            ),
            files=files,
        )

import fcntl
import gzip
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.deploy import rotation_engine as engine_module
from app.deploy.rotate_logs import main
from app.deploy.rotation_engine import RotationEngine
from app.deploy.rotation_models import PendingMetadata, RotationStatus
from app.deploy.rotation_fs import atomic_write_json


def _layout(tmp_path: Path) -> tuple[Settings, Path, Path, Path]:
    root = tmp_path / ".tg-hub"
    logs = root / "logs"
    runtime = root / "runtime"
    logs.mkdir(parents=True, mode=0o700)
    runtime.mkdir(mode=0o700)
    for directory in (root, logs, runtime):
        directory.chmod(0o700)
    heartbeat = runtime / "heartbeat.jsonl"
    for path in (logs / "app.stdout.log", logs / "app.stderr.log", heartbeat):
        path.write_bytes(b"")
        path.chmod(0o600)
    settings = Settings(
        APP_ENV="development",
        LOG_DIR=logs,
        HEARTBEAT_PATH=heartbeat,
    )
    return settings, logs, runtime, heartbeat


def _engine(
    settings: Settings,
    *,
    threshold: int = 1,
    budget: int = 250 * 1024 * 1024,
    retention=None,
) -> RotationEngine:
    return RotationEngine(
        settings,
        threshold_bytes=threshold,
        max_age=timedelta(hours=24),
        archive_budget_bytes=budget,
        retention=retention,
        now=lambda: datetime(2026, 7, 13, 9, 30, tzinfo=UTC),
    )


def _archives(logs: Path, target: str) -> list[Path]:
    return sorted((logs / "archive").glob(f"{target}.*.jsonl.gz"))


def test_dry_run_is_read_only(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    archive = logs / "archive"
    archive.mkdir(mode=0o700)
    active = logs / "app.stdout.log"
    active.write_text('{"event":"one"}\n', encoding="utf-8")
    before = active.read_bytes()

    result = _engine(settings).run(dry_run=True)

    assert result.status == "pass"
    assert active.read_bytes() == before
    assert not (runtime / "rotation-status.json").exists()
    assert list(archive.iterdir()) == []


def test_application_rotation_keeps_inode_and_commits_gzip(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    active = logs / "app.stdout.log"
    active.write_text('{"event":"one"}\n{"event":"two"}\n', encoding="utf-8")
    inode = active.stat().st_ino

    result = _engine(settings).run()

    assert result.status == "pass"
    assert active.stat().st_ino == inode
    assert active.read_bytes() == b""
    archive = _archives(logs, "app.stdout.log")
    assert len(archive) == 1
    with gzip.open(archive[0], "rt", encoding="utf-8") as handle:
        assert [json.loads(line)["event"] for line in handle] == ["one", "two"]
    status = RotationStatus.model_validate_json(
        (runtime / "rotation-status.json").read_text(encoding="utf-8")
    )
    assert status.files["app.stdout.log"].result == "rotated"
    assert status.files["app.stdout.log"].truncated_bytes > 0


def test_application_partial_tail_is_excluded_and_counted(tmp_path: Path) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    active = logs / "app.stderr.log"
    active.write_bytes(b'{"event":"ok"}\n{"partial":')

    result = _engine(settings).run()

    assert result.status == "pass"
    archive = _archives(logs, "app.stderr.log")[0]
    with gzip.open(archive, "rb") as handle:
        assert handle.read() == b'{"event":"ok"}\n'


def test_heartbeat_pending_is_not_modified_when_gzip_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, logs, _, heartbeat = _layout(tmp_path)
    original = b'{"heartbeat_total":1}\n{"partial":'
    heartbeat.write_bytes(original)
    engine = _engine(settings)

    def fail_gzip(source, final, token):
        raise engine_module.RotationFsError("ROTATION_COMPRESS_FAILED")

    monkeypatch.setattr(engine, "_gzip_atomic", fail_gzip)
    result = engine.run()

    assert result.status == "partial"
    pending = list((logs / "archive").glob("heartbeat.jsonl.*.pending"))
    assert len(pending) == 1
    assert pending[0].read_bytes() == original
    assert heartbeat.exists()
    assert heartbeat.read_bytes() == b""


def test_heartbeat_rotation_archives_complete_lines_and_switches_inode(
    tmp_path: Path,
) -> None:
    settings, logs, _, heartbeat = _layout(tmp_path)
    heartbeat.write_bytes(b'{"heartbeat_total":1}\n{"partial":')
    old_inode = heartbeat.stat().st_ino

    result = _engine(settings).run()

    assert result.status == "pass"
    assert heartbeat.stat().st_ino != old_inode
    archive = _archives(logs, "heartbeat.jsonl")[0]
    with gzip.open(archive, "rb") as handle:
        assert handle.read() == b'{"heartbeat_total":1}\n'
    heartbeat.write_bytes(b'{"heartbeat_total":2}\n')
    assert heartbeat.stat().st_size > 0


def _pending_paths(logs: Path, target: str, token: str):
    base = logs / "archive" / f"{target}.20260713T093000.000000Z.{token}.jsonl"
    return base.with_suffix(".jsonl.pending"), base.with_suffix(".jsonl.pending.meta")


def test_snapshot_committed_recovery_is_blocked_without_truncate(tmp_path: Path) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    archive = logs / "archive"
    archive.mkdir(mode=0o700)
    active = logs / "app.stdout.log"
    active.write_bytes(b'{"event":"same"}\n')
    token = "a" * 32
    pending, meta_path = _pending_paths(logs, "app.stdout.log", token)
    pending.write_bytes(active.read_bytes())
    pending.chmod(0o600)
    meta = PendingMetadata(
        target="app.stdout.log",
        run_id=token,
        phase="snapshot_committed",
        active_inode=active.stat().st_ino,
        snapshot_size=active.stat().st_size,
        complete_line_bytes=active.stat().st_size,
        created_at=datetime.now(UTC),
    )
    atomic_write_json(meta_path, meta.model_dump(mode="json"), token="meta")

    result = _engine(settings).run()

    assert result.status == "fail"
    assert result.error_code == "ROTATION_RECOVERY_REQUIRED"
    assert active.read_bytes() == b'{"event":"same"}\n'
    assert pending.exists() and meta_path.exists()
    assert not _archives(logs, "app.stdout.log")


def test_active_truncated_pending_is_recovered_without_retruncate(tmp_path: Path) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    archive = logs / "archive"
    archive.mkdir(mode=0o700)
    active = logs / "app.stdout.log"
    active.write_bytes(b'{"event":"new"}\n')
    token = "b" * 32
    pending, meta_path = _pending_paths(logs, "app.stdout.log", token)
    pending.write_bytes(b'{"event":"old"}\n')
    pending.chmod(0o600)
    meta = PendingMetadata(
        target="app.stdout.log",
        run_id=token,
        phase="active_truncated",
        active_inode=active.stat().st_ino,
        snapshot_size=pending.stat().st_size,
        complete_line_bytes=pending.stat().st_size,
        created_at=datetime.now(UTC),
    )
    atomic_write_json(meta_path, meta.model_dump(mode="json"), token="meta")

    result = _engine(settings, threshold=1024).run()

    assert result.status == "pass"
    assert active.read_bytes() == b'{"event":"new"}\n'
    with gzip.open(_archives(logs, "app.stdout.log")[0], "rb") as handle:
        assert handle.read() == b'{"event":"old"}\n'
    assert not pending.exists() and not meta_path.exists()


def test_phase_metadata_failure_does_not_truncate_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    active = logs / "app.stdout.log"
    original = b'{"event":"safe"}\n'
    active.write_bytes(original)
    real_write = engine_module.atomic_write_json

    def fail_meta(path, payload, *, token):
        if path.name.endswith(".pending.meta"):
            raise engine_module.RotationFsError("ROTATION_STATUS_WRITE_FAILED")
        return real_write(path, payload, token=token)

    monkeypatch.setattr(engine_module, "atomic_write_json", fail_meta)
    result = _engine(settings).run()

    assert result.status == "partial"
    assert active.read_bytes() == original
    assert list((logs / "archive").glob("app.stdout.log.*.pending"))


def test_rotate_lock_rejects_concurrent_run(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    (logs / "archive").mkdir(mode=0o700)
    lock = runtime / "rotate.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = _engine(settings).run()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result.status == "fail"
    assert result.error_code == "ROTATION_ALREADY_RUNNING"


def test_retention_keeps_fixed_count(tmp_path: Path) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    archive = logs / "archive"
    archive.mkdir(mode=0o700)
    for index in range(5):
        path = archive / (
            f"heartbeat.jsonl.20260713T09300{index}.000000Z."
            f"{index:032x}.jsonl.gz"
        )
        path.write_bytes(b"x")
        path.chmod(0o600)
        os.utime(path, (index + 1, index + 1))

    result = _engine(
        settings,
        threshold=1024,
        retention={
            "app.stdout.log": 7,
            "app.stderr.log": 7,
            "heartbeat.jsonl": 3,
        },
    ).run()

    assert result.status == "pass"
    assert len(_archives(logs, "heartbeat.jsonl")) == 3
    assert result.cleaned_archives == 2


def test_permissions_are_private(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    (logs / "app.stdout.log").write_text("{}\n", encoding="utf-8")

    assert _engine(settings).run().status == "pass"

    assert stat.S_IMODE((logs / "archive").stat().st_mode) == 0o700
    assert stat.S_IMODE((runtime / "rotate.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE((runtime / "rotation-status.json").stat().st_mode) == 0o600
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in _archives(logs, "app.stdout.log"))


def test_cli_emits_stable_json(monkeypatch, tmp_path: Path, capsys) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    (logs / "archive").mkdir(mode=0o700)
    monkeypatch.setattr("app.deploy.rotate_logs.load_settings", lambda: settings)

    exit_code = main(["--dry-run"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "pass"
    assert payload["report_desensitized"] == "yes"
    assert "path" not in payload


def _write_previous_status(runtime: Path, *, generation: datetime) -> None:
    now = datetime(2026, 7, 13, 9, 30, tzinfo=UTC)
    status = RotationStatus(
        run_id="f" * 32,
        check_started_at=now,
        check_completed_at=now,
        status="pass",
        files={
            target: {
                "active_generation_started_at": generation,
                "last_checked_at": generation,
            }
            for target in (
                "app.stdout.log",
                "app.stderr.log",
                "heartbeat.jsonl",
            )
        },
    )
    atomic_write_json(
        runtime / "rotation-status.json",
        status.model_dump(mode="json"),
        token="previous",
    )


def test_generation_age_triggers_nonempty_file(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    (logs / "archive").mkdir(mode=0o700)
    (logs / "app.stdout.log").write_text("{}\n", encoding="utf-8")
    _write_previous_status(
        runtime,
        generation=datetime(2026, 7, 11, tzinfo=UTC),
    )

    result = _engine(settings, threshold=1024).run()

    assert result.status == "pass"
    assert len(_archives(logs, "app.stdout.log")) == 1


def test_empty_file_does_not_rotate_by_age(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    (logs / "archive").mkdir(mode=0o700)
    _write_previous_status(
        runtime,
        generation=datetime(2026, 7, 11, tzinfo=UTC),
    )

    result = _engine(settings, threshold=1024).run()

    assert result.status == "pass"
    assert not _archives(logs, "app.stdout.log")


def test_symlink_active_is_rejected(tmp_path: Path) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    active = logs / "app.stdout.log"
    active.unlink()
    target = logs / "outside.log"
    target.write_text("{}\n", encoding="utf-8")
    active.symlink_to(target)

    result = _engine(settings).run()

    assert result.status == "partial"
    status = RotationStatus.model_validate_json(
        (settings.HEARTBEAT_PATH.parent / "rotation-status.json").read_text()
    )
    assert status.files["app.stdout.log"].error_code == (
        "ROTATION_ACTIVE_NOT_REGULAR"
    )


def test_symlink_archive_directory_is_rejected(tmp_path: Path) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    real = logs / "real-archive"
    real.mkdir(mode=0o700)
    (logs / "archive").symlink_to(real, target_is_directory=True)

    result = _engine(settings).run()

    assert result.status == "fail"
    assert result.error_code == "ROTATION_PATH_INVALID"


def test_leftover_unlocked_lock_file_does_not_block(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    (logs / "archive").mkdir(mode=0o700)
    lock = runtime / "rotate.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)

    assert _engine(settings).run(dry_run=True).status == "pass"


def test_heartbeat_pending_recovery_preserves_complete_lines(tmp_path: Path) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    archive = logs / "archive"
    archive.mkdir(mode=0o700)
    token = "c" * 32
    pending, _ = _pending_paths(logs, "heartbeat.jsonl", token)
    pending.write_bytes(b'{"heartbeat_total":1}\n{"partial":')
    pending.chmod(0o600)

    result = _engine(settings, threshold=1024).run()

    assert result.status == "pass"
    assert not pending.exists()
    with gzip.open(_archives(logs, "heartbeat.jsonl")[0], "rb") as handle:
        assert handle.read() == b'{"heartbeat_total":1}\n'


def test_active_truncated_metadata_failure_preserves_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    active = logs / "app.stdout.log"
    active.write_text("{}\n", encoding="utf-8")
    real_write = engine_module.atomic_write_json
    meta_writes = 0

    def fail_second_meta(path, payload, *, token):
        nonlocal meta_writes
        if path.name.endswith(".pending.meta"):
            meta_writes += 1
            if meta_writes == 2:
                raise engine_module.RotationFsError(
                    "ROTATION_STATUS_WRITE_FAILED"
                )
        return real_write(path, payload, token=token)

    monkeypatch.setattr(engine_module, "atomic_write_json", fail_second_meta)
    result = _engine(settings).run()

    assert result.status == "partial"
    assert active.read_bytes() == b""
    pending = list((logs / "archive").glob("app.stdout.log.*.pending"))
    assert len(pending) == 1
    meta = PendingMetadata.model_validate_json(
        pending[0].with_suffix(".pending.meta").read_text()
    )
    assert meta.phase == "snapshot_committed"
    assert not _archives(logs, "app.stdout.log")


def test_application_gzip_failure_keeps_active_truncated_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    active = logs / "app.stderr.log"
    active.write_text("{}\n", encoding="utf-8")
    engine = _engine(settings)
    monkeypatch.setattr(
        engine,
        "_gzip_atomic",
        lambda source, final, token: (_ for _ in ()).throw(
            engine_module.RotationFsError("ROTATION_COMPRESS_FAILED")
        ),
    )

    result = engine.run()

    assert result.status == "partial"
    pending = list((logs / "archive").glob("app.stderr.log.*.pending"))
    assert len(pending) == 1
    meta = PendingMetadata.model_validate_json(
        pending[0].with_suffix(".pending.meta").read_text()
    )
    assert meta.phase == "active_truncated"
    assert active.read_bytes() == b""


def test_archive_budget_deletes_only_final_archives(tmp_path: Path) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    archive = logs / "archive"
    archive.mkdir(mode=0o700)
    for index in range(3):
        path = archive / (
            f"app.stdout.log.20260713T09300{index}.000000Z."
            f"{index:032x}.jsonl.gz"
        )
        path.write_bytes(b"12345")
        path.chmod(0o600)
        os.utime(path, (index + 1, index + 1))
    pending, meta = _pending_paths(logs, "app.stderr.log", "d" * 32)
    pending.write_bytes(b"protected\n")
    pending.chmod(0o600)
    meta.write_text("{}", encoding="utf-8")
    meta.chmod(0o600)

    result = _engine(settings, threshold=1024, budget=4).run()

    assert result.status == "fail"
    assert result.error_code == "ROTATION_RECOVERY_REQUIRED"
    assert pending.exists() and meta.exists()
    assert len(_archives(logs, "app.stdout.log")) == 3


def test_rotation_status_contains_no_absolute_paths(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    (logs / "app.stdout.log").write_text("{}\n", encoding="utf-8")

    assert _engine(settings).run().status == "pass"
    serialized = (runtime / "rotation-status.json").read_text(encoding="utf-8")

    assert str(tmp_path) not in serialized
    assert "exception" not in serialized
    assert "pending" not in serialized


def test_nonregular_active_target_is_rejected(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    active = logs / "app.stderr.log"
    active.unlink()
    active.mkdir(mode=0o700)

    result = _engine(settings).run()

    assert result.status == "partial"
    status = RotationStatus.model_validate_json(
        (runtime / "rotation-status.json").read_text()
    )
    assert status.files["app.stderr.log"].error_code == (
        "ROTATION_ACTIVE_NOT_REGULAR"
    )


def test_archive_budget_removes_oldest_final_archives(tmp_path: Path) -> None:
    settings, logs, _, _ = _layout(tmp_path)
    archive = logs / "archive"
    archive.mkdir(mode=0o700)
    for index in range(3):
        path = archive / (
            f"app.stdout.log.20260713T09300{index}.000000Z."
            f"{index:032x}.jsonl.gz"
        )
        path.write_bytes(b"12345")
        path.chmod(0o600)
        os.utime(path, (index + 1, index + 1))

    result = _engine(settings, threshold=1024, budget=9).run()

    assert result.status == "pass"
    assert result.archive_budget_status == "cleaned"
    assert result.cleaned_archives == 2
    remaining = _archives(logs, "app.stdout.log")
    assert len(remaining) == 1
    assert "00000000000000000000000000000002" in remaining[0].name


def test_snapshot_copy_stops_at_initial_size(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    active = logs / "app.stdout.log"
    initial = b'{"event":"initial"}\n' + b"x" * (1024 * 1024)
    active.write_bytes(initial)
    real_read = engine_module.os.read
    appended = False

    def append_during_read(fd, size):
        nonlocal appended
        chunk = real_read(fd, size)
        if not appended:
            appended = True
            with active.open("ab") as handle:
                handle.write(b'{"event":"later"}\n')
        return chunk

    monkeypatch.setattr(engine_module.os, "read", append_during_read)
    result = _engine(settings).run()

    assert result.status == "pass"
    status = RotationStatus.model_validate_json(
        (runtime / "rotation-status.json").read_text()
    )
    assert status.files["app.stdout.log"].copied_bytes == len(initial)


def test_symlink_rotation_status_is_rejected(tmp_path: Path) -> None:
    settings, logs, runtime, _ = _layout(tmp_path)
    (logs / "archive").mkdir(mode=0o700)
    outside = runtime / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (runtime / "rotation-status.json").symlink_to(outside)

    result = _engine(settings, threshold=1024).run()

    assert result.status == "fail"
    assert result.error_code == "ROTATION_PATH_INVALID"
    assert outside.read_text(encoding="utf-8") == "{}"

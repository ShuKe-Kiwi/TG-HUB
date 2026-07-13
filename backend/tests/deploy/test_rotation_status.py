import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import subprocess
import sys

from app.config import Settings
from app.deploy.rotation_status import read_rotation_status


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _settings(tmp_path: Path) -> Settings:
    return Settings(HEARTBEAT_PATH=tmp_path / "runtime" / "heartbeat.jsonl")


def _write(path: Path, payload: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)


def _agent(installed_at: datetime) -> dict:
    return {
        "schema_version": 1,
        "label": "com.tghub.rotate-logs",
        "installed_at": installed_at.isoformat(),
    }


def _status(
    completed_at: datetime,
    *,
    result: str = "pass",
    error_code: str | None = None,
    legacy: bool = False,
) -> dict:
    return {
        "schema_version": 1,
        "run_id": "private-run-id",
        "check_started_at": (completed_at - timedelta(seconds=1)).isoformat(),
        "check_completed_at": completed_at.isoformat(),
        "status": result,
        "error_code": error_code,
        "rotated_files": 2,
        "cleaned_archives": 1,
        "archive_bytes": 100,
        "active_bytes": 20,
        "total_observed_bytes": 120,
        "archive_budget_status": "within_budget",
        "active_oversize": False,
        "files": {
            "app.stdout.log": {
                "archive_name": "must-not-leak.gz",
                "legacy_content_possible": legacy,
            }
        },
        "rotation_consistency": "best_effort_copy_truncate",
        "concurrent_write_loss_possible": True,
        "writer_paused": False,
        "report_desensitized": "yes",
    }


def test_missing_files_are_not_configured_never_run(tmp_path: Path) -> None:
    projection = read_rotation_status(_settings(tmp_path), now=NOW)

    assert projection.agent_status == "not_configured"
    assert projection.status == "never_run"
    assert projection.stale == "not_applicable"
    assert projection.rotated_files == 0


def test_never_run_uses_install_grace_period(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _write(runtime / "rotation-agent.json", _agent(NOW - timedelta(hours=1)))
    fresh = read_rotation_status(_settings(tmp_path), now=NOW)
    _write(runtime / "rotation-agent.json", _agent(NOW - timedelta(hours=3)))
    stale = read_rotation_status(_settings(tmp_path), now=NOW)

    assert fresh.status == "never_run" and fresh.stale is False
    assert stale.status == "never_run" and stale.stale is True


def test_previous_installation_status_is_not_current(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    installed = NOW - timedelta(minutes=30)
    _write(runtime / "rotation-agent.json", _agent(installed))
    _write(
        runtime / "rotation-status.json",
        _status(installed - timedelta(seconds=1), result="fail"),
    )

    projection = read_rotation_status(_settings(tmp_path), now=NOW)

    assert projection.status == "never_run"
    assert projection.last_started_at is None
    assert projection.last_completed_at is None
    assert projection.error_code is None


def test_equal_installation_boundary_is_current(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    installed = NOW - timedelta(minutes=30)
    _write(runtime / "rotation-agent.json", _agent(installed))
    _write(runtime / "rotation-status.json", _status(installed))

    projection = read_rotation_status(_settings(tmp_path), now=NOW)

    assert projection.status == "pass"
    assert projection.last_completed_at == installed


def test_valid_status_maps_counts_stale_and_legacy(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    installed = NOW - timedelta(hours=4)
    completed = NOW - timedelta(hours=3)
    _write(runtime / "rotation-agent.json", _agent(installed))
    _write(runtime / "rotation-status.json", _status(completed, legacy=True))

    projection = read_rotation_status(_settings(tmp_path), now=NOW)

    assert projection.status == "pass"
    assert projection.stale is True
    assert projection.rotated_files == 2
    assert projection.cleaned_archives == 1
    assert projection.legacy_content_possible is True
    serialized = projection.model_dump_json()
    assert "private-run-id" not in serialized
    assert "must-not-leak.gz" not in serialized


def test_fail_is_independent_from_stale(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _write(runtime / "rotation-agent.json", _agent(NOW - timedelta(hours=1)))
    _write(
        runtime / "rotation-status.json",
        _status(
            NOW - timedelta(minutes=1),
            result="fail",
            error_code="ROTATION_COMPRESS_FAILED",
        ),
    )

    projection = read_rotation_status(_settings(tmp_path), now=NOW)

    assert projection.status == "fail"
    assert projection.stale is False
    assert projection.error_code == "ROTATION_COMPRESS_FAILED"


def test_unknown_execution_error_is_not_exposed(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _write(runtime / "rotation-agent.json", _agent(NOW - timedelta(hours=1)))
    _write(
        runtime / "rotation-status.json",
        _status(NOW, result="fail", error_code="private exception text"),
    )

    projection = read_rotation_status(_settings(tmp_path), now=NOW)

    assert projection.error_code == "ROTATION_STATUS_ERROR_UNKNOWN"
    assert "private exception text" not in projection.model_dump_json()


def test_invalid_status_uses_null_unknown_values(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _write(runtime / "rotation-agent.json", _agent(NOW - timedelta(hours=1)))
    _write(runtime / "rotation-status.json", {"schema_version": 99})

    projection = read_rotation_status(_settings(tmp_path), now=NOW)

    assert projection.status == "invalid"
    assert projection.error_code == "ROTATION_STATUS_SCHEMA_UNSUPPORTED"
    assert projection.rotated_files is None
    assert projection.active_oversize is None
    assert projection.legacy_content_possible is None
    assert projection.stale == "unknown"


def test_invalid_metadata_has_priority(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _write(runtime / "rotation-agent.json", {"schema_version": 99})
    _write(runtime / "rotation-status.json", {"schema_version": 99})

    projection = read_rotation_status(_settings(tmp_path), now=NOW)

    assert projection.agent_status == "invalid"
    assert projection.error_code == "ROTATION_AGENT_METADATA_INVALID"


def test_reader_rejects_symlink_and_permissions_without_mutating(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside.json"
    _write(outside, _agent(NOW))
    runtime.mkdir(mode=0o700)
    metadata = runtime / "rotation-agent.json"
    metadata.symlink_to(outside)
    before = stat.S_IMODE(outside.stat().st_mode)

    symlinked = read_rotation_status(_settings(tmp_path), now=NOW)
    metadata.unlink()
    _write(metadata, _agent(NOW), mode=0o644)
    broad = read_rotation_status(_settings(tmp_path), now=NOW)

    assert symlinked.error_code == "ROTATION_AGENT_METADATA_INVALID"
    assert broad.error_code == "ROTATION_AGENT_METADATA_INVALID"
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o644
    assert before == stat.S_IMODE(outside.stat().st_mode)


def test_reader_rejects_oversize_and_invalid_time(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    metadata = runtime / "rotation-agent.json"
    metadata.write_bytes(b"{" + b"x" * (1024 * 1024) + b"}")
    metadata.chmod(0o600)
    oversized = read_rotation_status(_settings(tmp_path), now=NOW)
    _write(metadata, _agent(NOW.replace(tzinfo=None)))
    naive = read_rotation_status(_settings(tmp_path), now=NOW)

    assert oversized.error_code == "ROTATION_AGENT_METADATA_INVALID"
    assert naive.error_code == "ROTATION_AGENT_METADATA_INVALID"


def test_reader_does_not_create_runtime_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    read_rotation_status(settings, now=NOW)

    assert not (tmp_path / "runtime").exists()


def test_cli_exit_codes_only_evaluate_current_installation(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "production.env"
    env_file.write_text(
        f"HEARTBEAT_PATH={runtime}/heartbeat.jsonl\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["TG_HUB_ENV_FILE"] = str(env_file)

    not_configured = subprocess.run(
        [sys.executable, "-m", "app.deploy.rotation_status"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    _write(
        runtime / "rotation-status.json",
        _status(
            datetime.now(timezone.utc) - timedelta(days=1),
            result="fail",
            error_code="ROTATION_COMPRESS_FAILED",
        ),
    )
    historical_uninstalled_fail = subprocess.run(
        [sys.executable, "-m", "app.deploy.rotation_status"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    installed = datetime.now(timezone.utc) - timedelta(minutes=1)
    _write(runtime / "rotation-agent.json", _agent(installed))
    _write(
        runtime / "rotation-status.json",
        _status(
            installed,
            result="fail",
            error_code="ROTATION_COMPRESS_FAILED",
        ),
    )
    current_fail = subprocess.run(
        [sys.executable, "-m", "app.deploy.rotation_status"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    _write(runtime / "rotation-status.json", {"schema_version": 99})
    invalid = subprocess.run(
        [sys.executable, "-m", "app.deploy.rotation_status"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert not_configured.returncode == 0
    assert historical_uninstalled_fail.returncode == 0
    assert current_fail.returncode == 1
    assert invalid.returncode == 2
    for result in (
        not_configured,
        historical_uninstalled_fail,
        current_fail,
        invalid,
    ):
        assert json.loads(result.stdout)["report_desensitized"] == "yes"

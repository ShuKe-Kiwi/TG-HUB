import json
import os
from pathlib import Path
import stat
import subprocess

from app.config import Settings
from app.deploy.rotation_agent import record_install, remove_install


DEPLOY = Path(__file__).parents[2] / "deploy"


def _settings(tmp_path: Path) -> Settings:
    return Settings(HEARTBEAT_PATH=tmp_path / "runtime" / "heartbeat.jsonl")


def _write_fake_launchctl(bin_dir: Path, state: Path) -> None:
    script = bin_dir / "launchctl"
    script.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  print) [ -f \"$FAKE_LAUNCH_STATE\" ] ;;\n"
        "  bootstrap) : > \"$FAKE_LAUNCH_STATE\" ;;\n"
        "  bootout) rm -f \"$FAKE_LAUNCH_STATE\" ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _script_env(tmp_path: Path, env_file: Path, state: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_fake_launchctl(bin_dir, state)
    env = os.environ.copy()
    env.update(
        HOME=str(tmp_path),
        TG_HUB_ENV_FILE=str(env_file),
        FAKE_LAUNCH_STATE=str(state),
        PATH=f"{bin_dir}:{env['PATH']}",
    )
    return env


def test_record_install_is_atomic_private_and_desensitized(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    path = record_install(settings)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload.keys() == {"schema_version", "label", "installed_at"}
    assert payload["schema_version"] == 1
    assert payload["label"] == "com.tghub.rotate-logs"
    assert payload["installed_at"].endswith("+00:00")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    serialized = path.read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert "pid" not in serialized.casefold()
    assert not list(path.parent.glob(".*.tmp"))


def test_remove_install_preserves_status_and_archive(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    metadata = record_install(settings)
    status_path = metadata.parent / "rotation-status.json"
    archive = tmp_path / "logs" / "archive" / "kept.gz"
    archive.parent.mkdir(parents=True)
    status_path.write_text("{}\n", encoding="utf-8")
    archive.write_bytes(b"archive")

    remove_install(settings)

    assert not metadata.exists()
    assert status_path.exists()
    assert archive.exists()


def test_remove_install_is_idempotent_when_runtime_is_absent(tmp_path: Path) -> None:
    remove_install(_settings(tmp_path))

    assert not (tmp_path / "runtime").exists()


def test_metadata_failure_rolls_back_loaded_job_and_created_plist(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime_link = tmp_path / "runtime"
    runtime_link.symlink_to(outside, target_is_directory=True)
    env_file = tmp_path / "production.env"
    env_file.write_text(
        f"APP_ENV=production\nHEARTBEAT_PATH={runtime_link}/heartbeat.jsonl\n",
        encoding="utf-8",
    )
    state = tmp_path / "loaded"
    env = _script_env(tmp_path, env_file, state)

    result = subprocess.run(
        [DEPLOY / "install_rotation.sh"],
        cwd=DEPLOY.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    plist = tmp_path / "Library" / "LaunchAgents" / "com.tghub.rotate-logs.plist"
    assert result.returncode == 17
    assert "ROTATION_AGENT_METADATA_WRITE_FAILED" in result.stdout
    assert not state.exists()
    assert not plist.exists()
    assert not list(outside.glob(".*.tmp"))


def test_uninstall_script_removes_metadata_but_preserves_history(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    env_file = tmp_path / "production.env"
    env_file.write_text(
        f"APP_ENV=production\nHEARTBEAT_PATH={runtime}/heartbeat.jsonl\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    metadata = record_install(settings)
    status_path = runtime / "rotation-status.json"
    status_path.write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "logs" / "archive" / "kept.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"archive")
    state = tmp_path / "loaded"
    state.touch()
    env = _script_env(tmp_path, env_file, state)

    result = subprocess.run(
        [DEPLOY / "uninstall_rotation.sh"],
        cwd=DEPLOY.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert not state.exists()
    assert not metadata.exists()
    assert status_path.exists()
    assert archive.exists()

import os
from pathlib import Path
import subprocess


DEPLOY = Path(__file__).parents[2] / "deploy"


def test_launchd_template_has_locked_lifecycle_contract() -> None:
    content = (DEPLOY / "com.tghub.service.plist.template").read_text(encoding="utf-8")
    assert "com.tghub.service" in content
    assert "<key>RunAtLoad</key><true/>" in content
    assert "<key>SuccessfulExit</key><false/>" in content
    assert "<key>ThrottleInterval</key><integer>10</integer>" in content
    assert "TG_HUB_ENV_FILE" in content
    assert "__LOG_DIR__/app.stdout.log" in content
    assert "__LOG_DIR__/app.stderr.log" in content
    assert "__LOG_DIR__/app.log" not in content
    assert "__LOG_DIR__/app.error.log" not in content
    for secret in ("TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "DATABASE_URL"):
        assert secret not in content


def test_lifecycle_scripts_use_modern_launchctl_and_no_pid_truth() -> None:
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    stop = (DEPLOY / "stop.sh").read_text(encoding="utf-8")
    uninstall = (DEPLOY / "uninstall.sh").read_text(encoding="utf-8")
    assert "launchctl bootstrap" in install
    assert "SERVICE_ALREADY_INSTALLED" in install
    assert 'touch "$LOG_DIR/app.stdout.log" "$LOG_DIR/app.stderr.log"' in install
    assert 'chmod 600 "$LOG_DIR/app.stdout.log" "$LOG_DIR/app.stderr.log"' in install
    assert "launchctl bootout" in stop
    assert "launchctl bootout" in uninstall
    combined = install + stop + uninstall + (DEPLOY / "status.sh").read_text(encoding="utf-8")
    assert "launchctl load" not in combined
    assert "launchctl unload" not in combined
    assert ".pid" not in combined


def test_runtime_scripts_use_backend_virtualenv() -> None:
    for name in ("start.sh", "status.sh"):
        content = (DEPLOY / name).read_text(encoding="utf-8")
        assert '"$BACKEND/.venv/bin/python"' in content
        assert '"$BACKEND/../.venv/bin/python"' not in content


def test_runtime_uses_server_module_without_uvicorn_log_reconfiguration() -> None:
    runtime = (DEPLOY.parent / "app/deploy/runtime.py").read_text(encoding="utf-8")
    server = (DEPLOY.parent / "app/deploy/server.py").read_text(encoding="utf-8")
    assert '"-m", "app.deploy.server"' in runtime
    assert "log_config=None" in server
    assert "access_log=False" in server


def test_rotation_launchd_template_has_locked_schedule_contract() -> None:
    content = (DEPLOY / "com.tghub.rotate-logs.plist.template").read_text(
        encoding="utf-8"
    )
    assert "com.tghub.rotate-logs" in content
    assert "__PYTHON__" in content
    assert "<string>-m</string>" in content
    assert "<string>app.deploy.rotate_logs</string>" in content
    assert "<key>WorkingDirectory</key><string>__BACKEND_DIR__</string>" in content
    assert "<key>RunAtLoad</key><false/>" in content
    assert "<key>StartInterval</key><integer>3600</integer>" in content
    assert "<key>ProcessType</key><string>Background</string>" in content
    assert "<key>KeepAlive</key>" not in content
    assert content.count("<string>/dev/null</string>") == 2
    for secret in ("TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "DATABASE_URL"):
        assert secret not in content


def test_rotation_lifecycle_scripts_keep_3d1_boundary() -> None:
    install = (DEPLOY / "install_rotation.sh").read_text(encoding="utf-8")
    uninstall = (DEPLOY / "uninstall_rotation.sh").read_text(encoding="utf-8")
    status = (DEPLOY / "rotation_status.sh").read_text(encoding="utf-8")

    assert "launchctl bootstrap" in install
    assert "ROTATION_AGENT_ALREADY_INSTALLED" in install
    assert "ROTATION_AGENT_ROLLBACK_FAILED" in install
    assert "ROTATION_AGENT_METADATA_WRITE_FAILED" in install
    assert "app.deploy.rotation_agent record-install" in install
    assert "launchctl bootout" in install
    assert "launchctl bootout" in uninstall
    assert "app.deploy.rotation_agent remove-install" in uninstall
    assert "app.deploy.rotation_status" in status
    assert 'exec "$BACKEND/.venv/bin/python"' in status
    assert 'cd "$BACKEND"' in status

    combined = install + uninstall + status
    assert "launchctl kickstart" not in combined
    assert "--dry-run" not in status
    assert "app.deploy.rotate_logs --" not in combined
    assert "TELEGRAM_API_ID" not in combined
    assert "TELEGRAM_API_HASH" not in combined
    assert "jq " not in status
    assert "grep " not in status


def test_rotation_install_dry_run_does_not_create_user_files(tmp_path: Path) -> None:
    env_file = tmp_path / "production.env"
    env_file.write_text("APP_ENV=production\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(HOME=str(tmp_path), TG_HUB_ENV_FILE=str(env_file))

    result = subprocess.run(
        [DEPLOY / "install_rotation.sh", "--dry-run"],
        cwd=DEPLOY.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert '"dry_run":true' in result.stdout
    assert not (tmp_path / "Library").exists()


def test_rotation_install_never_overwrites_existing_plist(tmp_path: Path) -> None:
    env_file = tmp_path / "production.env"
    env_file.write_text("APP_ENV=production\n", encoding="utf-8")
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / "com.tghub.rotate-logs.plist"
    plist.write_text("owned-before-install", encoding="utf-8")
    env = os.environ.copy()
    env.update(HOME=str(tmp_path), TG_HUB_ENV_FILE=str(env_file))

    result = subprocess.run(
        [DEPLOY / "install_rotation.sh"],
        cwd=DEPLOY.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 10
    assert "ROTATION_AGENT_ALREADY_INSTALLED" in result.stdout
    assert plist.read_text(encoding="utf-8") == "owned-before-install"

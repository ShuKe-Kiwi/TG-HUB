from pathlib import Path


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

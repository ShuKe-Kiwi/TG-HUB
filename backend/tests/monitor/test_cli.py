import argparse
import io
import json

from app.config import Settings
from app.modules.monitor import cli
from app.modules.monitor.bootstrap import (
    MonitorAssemblyReport,
    MonitorBootstrapResult,
)
from app.modules.monitor.preflight import MonitorStartupPreflightReport
from app.modules.monitor.runtime import failed_monitor_runtime_summary


def _preflight(status: str = "pass") -> MonitorStartupPreflightReport:
    blockers = [] if status == "pass" else ["telegram_api_id_missing"]
    return MonitorStartupPreflightReport(
        status=status,
        watchlist_loaded="yes",
        watchlist_schema="pass",
        telethon_dependency="pass",
        telegram_api_id_configured="pass" if status == "pass" else "fail",
        telegram_api_hash_configured="pass",
        session_configured="pass",
        session_parent_exists="pass",
        session_parent_writable="pass",
        database_url_configured="pass",
        enabled_source_channels=1,
        invalid_source_channels=0,
        enabled_watch_titles=1,
        blockers=blockers,
    )


def _result(*, success: bool) -> MonitorBootstrapResult:
    summary = failed_monitor_runtime_summary("CONNECT_FAILED")
    if success:
        summary = summary.model_copy(
            update={
                "startup_status": "pass",
                "final_state": "stopped",
                "shutdown_reason": "operator_stop",
                "blockers": [],
                "errors": [],
            }
        )
    return MonitorBootstrapResult(
        assembly=MonitorAssemblyReport(
            database_ready="yes",
            bot_notification_status="disabled_config_missing",
            heartbeat_status="disabled",
            blockers=[],
        ),
        runtime=summary,
    )


class FakeBootstrap:
    result = _result(success=True)
    instances = []

    def __init__(self, *args, **kwargs):
        self.stop_calls = 0
        self.closed = False
        self.heartbeat_sink = kwargs.get("heartbeat_sink")
        self.__class__.instances.append(self)

    def stop(self):
        self.stop_calls += 1

    async def run(self):
        return self.result

    async def aclose(self):
        self.closed = True
        await self.heartbeat_sink.aclose()


def _args(**updates):
    values = {
        "summary_json": False,
        "heartbeat_jsonl": None,
        "no_bot_notify": False,
    }
    values.update(updates)
    return argparse.Namespace(**values)


async def test_run_command_outputs_machine_parseable_json(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(cli, "run_static_startup_preflight", lambda settings: _preflight())
    monkeypatch.setattr(cli, "MonitorBootstrap", FakeBootstrap)
    output = io.StringIO()

    exit_code = await cli.run_monitor_command(
        _args(summary_json=True),
        app_settings=Settings(HEARTBEAT_PATH=tmp_path / "heartbeat.jsonl"),
        stdout=output,
        install_signal_handlers=False,
    )

    payload = json.loads(output.getvalue())
    assert exit_code == 0
    assert payload["final_state"] == "stopped"
    assert payload["bot_notification_status"] == "disabled_config_missing"
    assert FakeBootstrap.instances[-1].closed is True
    assert FakeBootstrap.instances[-1].heartbeat_sink.path == (
        tmp_path / "heartbeat.jsonl"
    )


async def test_run_command_preflight_blocker_returns_two(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "run_static_startup_preflight",
        lambda settings: _preflight("fail"),
    )
    output = io.StringIO()

    exit_code = await cli.run_monitor_command(
        _args(summary_json=True),
        app_settings=Settings(),
        stdout=output,
        install_signal_handlers=False,
    )

    assert exit_code == 2
    assert json.loads(output.getvalue())["status"] == "fail"


async def test_run_command_runtime_failure_returns_one(monkeypatch) -> None:
    monkeypatch.setattr(cli, "run_static_startup_preflight", lambda settings: _preflight())
    FakeBootstrap.result = _result(success=False)
    monkeypatch.setattr(cli, "MonitorBootstrap", FakeBootstrap)

    exit_code = await cli.run_monitor_command(
        _args(),
        app_settings=Settings(),
        stdout=io.StringIO(),
        install_signal_handlers=False,
    )

    assert exit_code == 1
    FakeBootstrap.result = _result(success=True)


def test_cli_import_and_parser_have_no_runtime_side_effects() -> None:
    parsed = cli.build_parser().parse_args(["run", "--no-bot-notify"])
    assert parsed.command == "run"
    assert parsed.no_bot_notify is True

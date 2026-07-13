"""Terminal entry point for static preflight and long-running monitor."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from app.config import Settings
from app.infra.logger import setup_logging
from app.modules.monitor.bootstrap import MonitorBootstrap, MonitorBootstrapResult
from app.modules.monitor.bootstrap import MonitorAssemblyReport
from app.modules.monitor.heartbeat import (
    create_heartbeat_sinks,
)
from app.modules.monitor.preflight import (
    MonitorStartupPreflightReport,
    run_static_startup_preflight,
)
from app.modules.monitor.runtime import failed_monitor_runtime_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg-hub-monitor")
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight")
    preflight.add_argument("--preflight-json", action="store_true")

    run = commands.add_parser("run")
    run.add_argument("--summary-json", action="store_true")
    run.add_argument("--heartbeat-jsonl", type=Path)
    run.add_argument("--no-bot-notify", action="store_true")
    return parser


def _print_preflight(
    report: MonitorStartupPreflightReport,
    *,
    json_output: bool,
    stream: TextIO,
) -> None:
    if json_output:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False), file=stream)
        return
    print("TG-HUB_MONITOR_PREFLIGHT_RESULT:", file=stream)
    for key, value in report.model_dump(mode="json").items():
        print(f"- {key}: {_human_value(value)}", file=stream)


def _print_run_result(
    result: MonitorBootstrapResult,
    *,
    json_output: bool,
    preflight: MonitorStartupPreflightReport,
    stream: TextIO,
) -> None:
    payload = result.runtime.model_dump(mode="json")
    payload.update(
        {
            "preflight_status": preflight.status,
            "watchlist_loaded": preflight.watchlist_loaded,
            **result.assembly.model_dump(mode="json"),
        }
    )
    if json_output:
        print(json.dumps(payload, ensure_ascii=False), file=stream)
        return

    last_error = result.runtime.errors[-1].error_code if result.runtime.errors else None
    fields = {
        "startup_status": result.runtime.startup_status,
        "final_state": result.runtime.final_state,
        "shutdown_reason": result.runtime.shutdown_reason,
        "uptime_seconds": round(result.runtime.uptime_seconds, 3),
        "preflight_status": preflight.status,
        "watchlist_loaded": preflight.watchlist_loaded,
        "enabled_channel_count": result.runtime.enabled_source_channels,
        "resolved_channel_count": result.runtime.resolved_channel_count,
        "ingestion_boundary_ready": result.assembly.ingestion_boundary_ready,
        "processing_boundary_ready": result.assembly.processing_boundary_ready,
        "event_bus_ready": result.assembly.event_bus_ready,
        "bot_notification_status": result.assembly.bot_notification_status,
        "heartbeat_status": result.assembly.heartbeat_status,
        "events_seen_total": result.runtime.events_seen_total,
        "events_matched_total": result.runtime.events_matched_total,
        "ingest_stored_total": result.runtime.ingest_stored_total,
        "ingest_duplicate_total": result.runtime.ingest_duplicate_total,
        "ingest_rejected_total": result.runtime.ingest_rejected_total,
        "ingest_failed_total": result.runtime.ingest_failed_total,
        "process_success_total": result.runtime.process_success_total,
        "process_already_done_total": result.runtime.process_already_done_total,
        "process_failed_total": result.runtime.process_failed_total,
        "last_error_code": last_error,
        "blockers": list(dict.fromkeys(result.runtime.blockers + result.assembly.blockers)),
    }
    print("TG-HUB_MONITOR_RUN_RESULT:", file=stream)
    for key, value in fields.items():
        print(f"- {key}: {_human_value(value)}", file=stream)


def _human_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "[]"
    if value is None:
        return "none"
    return str(value)


async def run_monitor_command(
    args: argparse.Namespace,
    *,
    app_settings: Settings,
    stdout: TextIO,
    install_signal_handlers: bool = True,
) -> int:
    preflight = run_static_startup_preflight(app_settings)
    if preflight.status == "fail":
        _print_preflight(preflight, json_output=args.summary_json, stream=stdout)
        return 2

    heartbeat_path = args.heartbeat_jsonl or app_settings.HEARTBEAT_PATH
    if app_settings.APP_ENV == "production":
        private_root = (Path.home() / ".tg-hub").resolve()
        try:
            Path(heartbeat_path).expanduser().resolve().relative_to(private_root)
        except (OSError, ValueError):
            raise ValueError("HEARTBEAT_PATH_OUTSIDE_PRIVATE_ROOT") from None
    sink, _ = create_heartbeat_sinks(heartbeat_path)
    bootstrap = MonitorBootstrap(
        app_settings,
        heartbeat_sink=sink,
        no_bot_notify=args.no_bot_notify,
    )
    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    first_signal: str | None = None

    def request_shutdown(signal_name: str) -> None:
        nonlocal first_signal
        if shutdown_requested.is_set():
            return
        first_signal = signal_name
        shutdown_requested.set()
        bootstrap.stop()

    installed: list[signal.Signals] = []
    if install_signal_handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, request_shutdown, signum.name.lower())
                installed.append(signum)
            except (NotImplementedError, RuntimeError):
                pass

    try:
        try:
            result = await bootstrap.run()
        except Exception:
            result = MonitorBootstrapResult(
                assembly=MonitorAssemblyReport(
                    database_ready="no",
                    bot_notification_status=(
                        "disabled_by_flag"
                        if args.no_bot_notify
                        else "disabled_config_missing"
                    ),
                    heartbeat_status=(
                        "write_failed" if sink.error_count else "enabled"
                    ),
                    blockers=["startup_exception"],
                ),
                runtime=failed_monitor_runtime_summary(
                    "UNEXPECTED_EXCEPTION"
                ),
            )
        _print_run_result(
            result,
            json_output=args.summary_json,
            preflight=preflight,
            stream=stdout,
        )
        if result.runtime.startup_status == "fail" or result.runtime.final_state == "failed":
            return 1
        return 0
    except asyncio.CancelledError:
        if first_signal == "sigint":
            return 130
        raise
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
        await bootstrap.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    app_settings = Settings()

    if args.command == "preflight":
        report = run_static_startup_preflight(app_settings)
        _print_preflight(
            report,
            json_output=args.preflight_json,
            stream=sys.stdout,
        )
        return 0 if report.status == "pass" else 2

    try:
        return asyncio.run(
            run_monitor_command(
                args,
                app_settings=app_settings,
                stdout=sys.stdout,
            )
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            f"monitor startup failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

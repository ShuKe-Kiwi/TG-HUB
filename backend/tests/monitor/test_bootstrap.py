import json
from pathlib import Path

from app.config import Settings
from app.modules.monitor.bootstrap import MonitorBootstrap
from app.modules.monitor.resolver import ChannelResolveResult
from app.modules.monitor.runtime import failed_monitor_runtime_summary


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    async def execute(self, statement):
        self.calls.append(str(statement))


class FakeSessionContext:
    def __init__(self, calls):
        self.session = FakeSession(calls)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeResolver:
    connected = False

    async def __aenter__(self):
        self.connected = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.connected = False

    async def resolve(self, ref, *, input_type):
        assert self.connected is True
        return ChannelResolveResult(
            input_ref=ref,
            input_type=input_type,
            status="resolved",
            numeric_channel_id=-1001234567890,
        )


class FakeRuntime:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.stopped = False
        self.__class__.instances.append(self)

    def stop(self):
        self.stopped = True

    async def run(self):
        summary = failed_monitor_runtime_summary("CONNECT_FAILED")
        return summary.model_copy(
            update={
                "startup_status": "pass",
                "final_state": "stopped",
                "shutdown_reason": "operator_stop",
                "blockers": [],
                "errors": [],
                "production_ingest_enabled": "yes",
                "processing_enabled": "yes",
            }
        )


async def test_bootstrap_checks_db_and_injects_application_boundaries(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "source_channels": [{"ref": "@example", "enabled": True}],
                "watch_titles": [{"title": "家业", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )
    calls = []
    configured = Settings(
        WATCHLIST_PATH=watchlist,
        TELEGRAM_API_ID=1,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_NAME=str(tmp_path / "session"),
        TELEGRAM_NOTIFY_CHAT_IDS="",
    )
    bootstrap = MonitorBootstrap(
        configured,
        session_factory=lambda: FakeSessionContext(calls),
        resolver_factory=lambda settings: FakeResolver(),
        client_factory=lambda settings: object(),
        runtime_factory=FakeRuntime,
    )

    result = await bootstrap.run()
    await bootstrap.aclose()

    assert calls == ["SELECT 1"]
    assert result.runtime.startup_status == "pass"
    assert result.assembly.database_ready == "yes"
    assert result.assembly.bot_notification_status == "disabled_config_missing"
    runtime = FakeRuntime.instances[-1]
    assert runtime.kwargs["ingestion_boundary"] is not None
    assert runtime.kwargs["processing_boundary"] is not None
    assert runtime.kwargs["resolved_channel_ids"] == (-1001234567890,)


def test_no_bot_notify_takes_priority_over_complete_config(tmp_path: Path) -> None:
    configured = Settings(
        TELEGRAM_BOT_TOKEN="bot-secret",
        TELEGRAM_NOTIFY_CHAT_IDS="1001",
    )
    bootstrap = MonitorBootstrap(configured, no_bot_notify=True)

    assert bootstrap._bot_status() == "disabled_by_flag"

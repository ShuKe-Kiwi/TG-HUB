import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.modules.monitor.online_preflight import OnlineSessionPreflightService
from app.modules.monitor.preflight import StaticStartupPreflight
from app.modules.monitor.session_ownership import SessionOwnershipLease


class Entity:
    def __init__(self, channel_id: int):
        self.channel_id = channel_id


class FakeClient:
    def __init__(self, *, authorized=True, fail_ref=None):
        self.authorized = authorized
        self.fail_ref = fail_ref
        self.connected = False
        self.disconnected = False
        self.queries = []

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self.authorized

    async def get_input_entity(self, peer):
        self.queries.append(peer)
        if peer == self.fail_ref:
            raise RuntimeError("hidden remote detail")
        if isinstance(peer, int):
            return Entity(abs(peer) % 10**6)
        return Entity(123456)

    async def disconnect(self):
        self.disconnected = True


def _settings(tmp_path: Path, refs=None) -> Settings:
    tmp_path.chmod(0o700)
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "source_channels": [
                    {"ref": ref, "enabled": True}
                    for ref in (refs or ["-100123456", "@example"])
                ],
                "watch_titles": [{"title": "百花杀", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )
    session = tmp_path / "account.session"
    session.write_bytes(b"")
    session.chmod(0o600)
    return Settings(
        WATCHLIST_PATH=watchlist,
        TELEGRAM_API_ID=1,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_NAME=str(session),
        HEARTBEAT_PATH=tmp_path / "runtime" / "heartbeat.jsonl",
        DATABASE_URL="postgresql+asyncpg://localhost/test",
    )


def _service(configured, **kwargs):
    return OnlineSessionPreflightService(
        configured,
        static_preflight_builder=lambda snapshot, settings: (
            StaticStartupPreflight(
                settings, dependency_probe=lambda: True
            ).run(snapshot)
        ),
        **kwargs,
    )


async def test_online_preflight_authorizes_and_resolves_numeric_via_client(tmp_path: Path):
    configured = _settings(tmp_path)
    client = FakeClient()
    report = await _service(
        configured, client_factory=lambda settings: client
    ).run()

    assert report.status == "pass"
    assert report.resolved_channels == 2
    assert report.failed_channels == 0
    assert report.telegram_api_accessed == "yes"
    assert client.queries == [-100123456, "example"]
    assert client.disconnected is True


async def test_online_preflight_reports_session_in_use_without_client(tmp_path: Path):
    configured = _settings(tmp_path)
    owner = SessionOwnershipLease.from_settings(configured)
    owner.acquire()
    created = False

    def create_client(settings):
        nonlocal created
        created = True
        return FakeClient()

    try:
        report = await _service(
            configured, client_factory=create_client
        ).run()
    finally:
        owner.release()

    assert report.error_code == "SESSION_IN_USE"
    assert report.telegram_api_accessed == "no"
    assert created is False


async def test_online_preflight_unauthorized_leaves_channels_unattempted(tmp_path: Path):
    configured = _settings(tmp_path)
    report = await _service(
        configured,
        client_factory=lambda settings: FakeClient(authorized=False),
    ).run()

    assert report.error_code == "SESSION_UNAUTHORIZED"
    assert report.channel_resolution == "blocked_by_session"
    assert report.unattempted_channels == 2


async def test_online_preflight_releases_only_after_cancelled_disconnect_settles(tmp_path: Path):
    configured = _settings(tmp_path, refs=["@example"])
    disconnect_started = asyncio.Event()
    allow_disconnect = asyncio.Event()

    class SlowDisconnectClient(FakeClient):
        async def disconnect(self):
            disconnect_started.set()
            await allow_disconnect.wait()
            self.disconnected = True

    service = _service(
        configured,
        client_factory=lambda settings: SlowDisconnectClient(),
        disconnect_timeout=30,
    )
    task = asyncio.create_task(service.run())
    await disconnect_started.wait()
    task.cancel()
    competitor = SessionOwnershipLease.from_settings(configured)
    with pytest.raises(Exception) as caught:
        competitor.acquire()
    assert getattr(caught.value, "error_code", None) == "SESSION_IN_USE"
    allow_disconnect.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    competitor.acquire()
    competitor.release()

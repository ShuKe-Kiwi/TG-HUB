from dataclasses import dataclass

import pytest

from app.config import Settings
from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.resolver import ChannelResolveError
from app.modules.monitor.telethon_resolver import (
    TelethonControlledChannelResolver,
    resolve_watchlist_once,
)


@dataclass
class FakeEntity:
    id: int
    username: str
    title: str


class FakeTelethonClient:
    def __init__(self, entities=None, errors=None) -> None:
        self.entities = entities or {}
        self.errors = errors or {}
        self.connected = False
        self.disconnected = False
        self.queries: list[str] = []
        self.handler_registered = False
        self.run_until_disconnected_called = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def get_entity(self, query: str):
        self.queries.append(query)
        if query in self.errors:
            raise self.errors[query]
        return self.entities[query]

    def add_event_handler(self, *args, **kwargs):
        self.handler_registered = True
        raise AssertionError("P6-2C-0B-2 must not register handlers")

    async def run_until_disconnected(self):
        self.run_until_disconnected_called = True
        raise AssertionError("P6-2C-0B-2 must not run listeners")


def _peer_id(entity: FakeEntity) -> int:
    return int(f"-100{entity.id}")


@pytest.mark.asyncio
async def test_resolve_watchlist_once_connects_resolves_and_disconnects() -> None:
    client = FakeTelethonClient(
        entities={
            "demo_channel": FakeEntity(
                id=12345,
                username="demo_channel",
                title="Demo Channel",
            ),
            "another_channel": FakeEntity(
                id=67890,
                username="another_channel",
                title="Another Channel",
            ),
        }
    )
    resolver = TelethonControlledChannelResolver(client, get_peer_id=_peer_id)
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "https://t.me/demo_channel"},
            {"ref": "@another_channel"},
        ],
        watch_titles=[],
    )

    report = await resolve_watchlist_once(watchlist, resolver)

    assert client.connected is True
    assert client.disconnected is True
    assert client.queries == ["demo_channel", "another_channel"]
    assert client.handler_registered is False
    assert client.run_until_disconnected_called is False
    assert report.resolved_count == 2
    assert [item.numeric_channel_id for item in report.results] == [
        -10012345,
        -10067890,
    ]
    assert report.listener_started == "no"
    assert report.handler_registered == "no"
    assert report.long_running_process == "no"


@pytest.mark.asyncio
async def test_numeric_refs_do_not_call_telethon_get_entity() -> None:
    client = FakeTelethonClient()
    resolver = TelethonControlledChannelResolver(client, get_peer_id=_peer_id)
    watchlist = WatchlistConfig(
        source_channels=[{"ref": "-10012345"}],
        watch_titles=[],
    )

    report = await resolve_watchlist_once(watchlist, resolver)

    assert client.connected is True
    assert client.disconnected is True
    assert client.queries == []
    assert report.already_numeric_count == 1
    assert report.results[0].numeric_channel_id == -10012345


@pytest.mark.asyncio
async def test_disconnect_runs_when_resolution_raises() -> None:
    class UnexpectedAdapterError(Exception):
        pass

    client = FakeTelethonClient(
        errors={"broken_channel": UnexpectedAdapterError("boom")}
    )
    resolver = TelethonControlledChannelResolver(client, get_peer_id=_peer_id)
    watchlist = WatchlistConfig(
        source_channels=[{"ref": "@broken_channel"}],
        watch_titles=[],
    )

    report = await resolve_watchlist_once(watchlist, resolver)

    assert client.connected is True
    assert client.disconnected is True
    assert report.resolver_error_count == 1
    assert report.results[0].error_code == "RPC_ERROR"


@pytest.mark.asyncio
async def test_error_mapping_uses_stable_codes() -> None:
    UsernameNotOccupiedError = type("UsernameNotOccupiedError", (Exception,), {})
    ChannelPrivateError = type("ChannelPrivateError", (Exception,), {})
    FloodWaitError = type("FloodWaitError", (Exception,), {})
    client = FakeTelethonClient(
        errors={
            "missing_user": UsernameNotOccupiedError("missing"),
            "private_user": ChannelPrivateError("private"),
            "flood_user": FloodWaitError("wait"),
        }
    )
    resolver = TelethonControlledChannelResolver(client, get_peer_id=_peer_id)
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "@missing_user"},
            {"ref": "@private_user"},
            {"ref": "@flood_user"},
        ],
        watch_titles=[],
    )

    report = await resolve_watchlist_once(watchlist, resolver)

    assert report.not_found_count == 1
    assert report.private_or_forbidden_count == 1
    assert report.resolver_error_count == 1
    assert [item.error_code for item in report.results] == [
        "USERNAME_NOT_FOUND",
        "CHANNEL_PRIVATE",
        "FLOOD_WAIT",
    ]


def test_from_settings_requires_telethon_credentials() -> None:
    with pytest.raises(ChannelResolveError) as exc_info:
        TelethonControlledChannelResolver.from_settings(
            Settings(
                TELETHON_API_ID=None,
                TELETHON_API_HASH="",
            )
        )

    assert exc_info.value.status == "resolver_unavailable"
    assert exc_info.value.error_code == "SESSION_UNAVAILABLE"


def test_adapter_module_does_not_expose_listener_api_terms() -> None:
    import app.modules.monitor.telethon_resolver as module

    source_names = set(dir(module))

    assert "NewMessage" not in source_names
    assert "events" not in source_names

"""P5-E MVP-B application assembly and end-to-end acceptance tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.config import Settings
from app.infra.events import RawMessageFailed
from app.main import app as global_app
from app.main import create_app
from app.modules.channel.schema import ChannelCreate
from app.modules.channel.service import ChannelService
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.rawmessage.service import RawMessageService
from app.modules.resource.model import (
    Resource,
    ResourceLink,
    ResourceSource,
    Work,
)
from app.modules.resource.query_service import ResourceQueryService

_SECRET_HEADER = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
_NOW = datetime(2026, 7, 2, 16, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SentMessage:
    chat_id: int
    text: str
    parse_mode: str


class FakeBotTransport:
    def __init__(self, failing_chat_ids: set[int] | None = None) -> None:
        self.messages: list[SentMessage] = []
        self.attempted_chat_ids: list[int] = []
        self.failing_chat_ids = failing_chat_ids or set()
        self.closed = False

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
    ) -> None:
        self.attempted_chat_ids.append(chat_id)
        if chat_id in self.failing_chat_ids:
            raise RuntimeError("fake transport failure")
        self.messages.append(
            SentMessage(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        )

    async def aclose(self) -> None:
        self.closed = True


class BareFakeBotTransport:
    """Fake transport intentionally lacking close/aclose."""

    def __init__(self) -> None:
        self.messages: list[SentMessage] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
    ) -> None:
        self.messages.append(
            SentMessage(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        )


class GuardSessionFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise AssertionError("session factory must not be called")


@pytest.fixture(autouse=True)
def block_real_telegram_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_real_network(*args, **kwargs):
        raise AssertionError("real network is forbidden in P5-E tests")

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        fail_real_network,
    )


def _settings(
    *,
    secret: str = "test-secret",
    allowed_chat_ids: str = "1001",
    notify_chat_ids: str = "2001",
) -> Settings:
    return Settings(
        APP_NAME="tg-hub",
        APP_ENV="test",
        TELEGRAM_WEBHOOK_SECRET=secret,
        TELEGRAM_ALLOWED_CHAT_IDS=allowed_chat_ids,
        TELEGRAM_NOTIFY_CHAT_IDS=notify_chat_ids,
    )


def _session_factory(
    db_session: AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    bind = db_session.bind
    assert bind is not None
    return async_sessionmaker(
        bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )


def _telegram_payload(
    text: str,
    *,
    chat_id: int = 1001,
) -> dict[str, object]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 3, "username": "ignored"},
            "text": text,
        },
    }


@asynccontextmanager
async def _running_client(application) -> AsyncIterator[httpx.AsyncClient]:
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


async def _seed_resource(
    db_session: AsyncSession,
) -> Resource:
    work = Work(
        title="家业",
        title_norm="家业",
        type="drama",
        aliases=[],
        year=2026,
        work_key="drama:家业",
        status="active",
    )
    db_session.add(work)
    await db_session.flush()

    resource = Resource(
        work_id=work.id,
        title="家业",
        title_norm="家业",
        resource_type="single_episode",
        episode_no=1,
        season_no=1,
        episode_range="ep1",
        year=2026,
        quality="1080p",
        resource_key="drama:家业:ep1",
        episode_key="drama:家业:s01e1",
        content_fingerprint="a" * 64,
        description="家业简介",
        tags=["drama"],
        status="active",
        source_count=0,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
    )
    db_session.add(resource)
    await db_session.commit()
    return resource


async def _ingest_parse_dedup(
    db_session: AsyncSession,
    *,
    event_bus,
    raw_text: str,
    tg_id: int,
) -> RawMessage:
    channel = await ChannelService(db_session).create_or_get(
        ChannelCreate(name=f"P5-E channel {tg_id}", tg_id=tg_id),
    )
    message = await RawMessageService(db_session).ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=tg_id,
            raw_text=raw_text,
        )
    )
    parsed = await RawMessageService(db_session).parse_and_persist(message.id)
    return await RawMessageService(db_session).dedup_and_persist(
        parsed.id,
        event_bus=event_bus,
    )


async def _count(db_session: AsyncSession, model: type) -> int:
    result = await db_session.execute(select(func.count(model.id)))
    return result.scalar_one()


def test_global_app_factory_compatibility() -> None:
    paths = set()
    for route in global_app.routes:
        if hasattr(route, "path"):
            paths.add(route.path)
        included_router = getattr(route, "original_router", None)
        if included_router is not None:
            paths.update(
                child.path
                for child in included_router.routes
                if hasattr(child, "path")
            )

    assert "/health" in paths
    assert "/telegram/webhook" in paths


@pytest.mark.asyncio
async def test_missing_token_keeps_health_and_disables_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    session_factory = GuardSessionFactory()
    application = create_app(
        app_settings=_settings(),
        session_factory=session_factory,  # type: ignore[arg-type]
    )

    async with _running_client(application) as client:
        health = await client.get("/health")
        webhook = await client.post(
            "/telegram/webhook",
            headers=_SECRET_HEADER,
            json=_telegram_payload("/help"),
        )

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "app": "tg-hub",
        "env": "test",
    }
    assert webhook.status_code == 503
    assert session_factory.calls == 0


@pytest.mark.asyncio
async def test_missing_secret_disables_webhook() -> None:
    fake_transport = FakeBotTransport()
    application = create_app(
        app_settings=_settings(secret=""),
        bot_transport=fake_transport,
        session_factory=GuardSessionFactory(),  # type: ignore[arg-type]
    )

    async with _running_client(application) as client:
        health = await client.get("/health")
        webhook = await client.post(
            "/telegram/webhook",
            json=_telegram_payload("/help"),
        )

    assert health.status_code == 200
    assert webhook.status_code == 503
    assert fake_transport.messages == []
    assert fake_transport.closed is True


@pytest.mark.asyncio
async def test_invalid_chat_id_configuration_disables_webhook() -> None:
    fake_transport = FakeBotTransport()
    application = create_app(
        app_settings=_settings(allowed_chat_ids="1001,invalid"),
        bot_transport=fake_transport,
        session_factory=GuardSessionFactory(),  # type: ignore[arg-type]
    )

    async with _running_client(application) as client:
        response = await client.post(
            "/telegram/webhook",
            headers=_SECRET_HEADER,
            json=_telegram_payload("/help"),
        )

    assert response.status_code == 503
    assert fake_transport.messages == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_secret_precedes_body_and_session_creation(
    headers: dict[str, str],
) -> None:
    session_factory = GuardSessionFactory()
    fake_transport = FakeBotTransport()
    application = create_app(
        app_settings=_settings(),
        bot_transport=fake_transport,
        session_factory=session_factory,  # type: ignore[arg-type]
    )

    async with _running_client(application) as client:
        response = await client.post(
            "/telegram/webhook",
            headers=headers,
            content=b"this is deliberately invalid JSON",
        )

    assert response.status_code == 403
    assert session_factory.calls == 0
    assert fake_transport.messages == []


@pytest.mark.asyncio
async def test_correct_secret_reaches_command_handler(
    db_session: AsyncSession,
) -> None:
    fake_transport = FakeBotTransport()
    application = create_app(
        app_settings=_settings(),
        bot_transport=fake_transport,
        session_factory=_session_factory(db_session),
    )

    async with _running_client(application) as client:
        response = await client.post(
            "/telegram/webhook",
            headers=_SECRET_HEADER,
            json=_telegram_payload("/help"),
        )
        assert fake_transport.closed is False

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "<b>可用命令</b>" in fake_transport.messages[0].text
    assert fake_transport.closed is True


@pytest.mark.asyncio
async def test_shutdown_accepts_fake_transport_without_close() -> None:
    fake_transport = BareFakeBotTransport()
    application = create_app(
        app_settings=_settings(),
        bot_transport=fake_transport,
        session_factory=GuardSessionFactory(),  # type: ignore[arg-type]
    )

    async with application.router.lifespan_context(application):
        assert application.state.telegram_runtime.enabled is True

    assert fake_transport.messages == []


@pytest.mark.asyncio
async def test_unauthorized_chat_does_not_query_or_send(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_calls = 0

    async def forbidden_query(*args, **kwargs):
        nonlocal query_calls
        query_calls += 1
        raise AssertionError("unauthorized chat reached query service")

    monkeypatch.setattr(ResourceQueryService, "latest", forbidden_query)
    monkeypatch.setattr(ResourceQueryService, "search", forbidden_query)
    monkeypatch.setattr(ResourceQueryService, "get_detail", forbidden_query)

    fake_transport = FakeBotTransport()
    application = create_app(
        app_settings=_settings(allowed_chat_ids="1001"),
        bot_transport=fake_transport,
        session_factory=_session_factory(db_session),
    )

    async with _running_client(application) as client:
        response = await client.post(
            "/telegram/webhook",
            headers=_SECRET_HEADER,
            json=_telegram_payload("/latest", chat_id=9999),
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert query_calls == 0
    assert fake_transport.messages == []
    assert fake_transport.attempted_chat_ids == []


@pytest.mark.asyncio
async def test_webhook_routes_latest_search_and_resource(
    db_session: AsyncSession,
) -> None:
    resource = await _seed_resource(db_session)
    fake_transport = FakeBotTransport()
    application = create_app(
        app_settings=_settings(),
        bot_transport=fake_transport,
        session_factory=_session_factory(db_session),
    )

    async with _running_client(application) as client:
        latest = await client.post(
            "/telegram/webhook",
            headers=_SECRET_HEADER,
            json=_telegram_payload("/latest"),
        )
        search = await client.post(
            "/telegram/webhook",
            headers=_SECRET_HEADER,
            json=_telegram_payload("/search 家业"),
        )
        detail = await client.post(
            "/telegram/webhook",
            headers=_SECRET_HEADER,
            json=_telegram_payload(f"/resource {resource.id}"),
        )

    assert latest.json() == {"ok": True}
    assert search.json() == {"ok": True}
    assert detail.json() == {"ok": True}
    assert [message.chat_id for message in fake_transport.messages] == [
        1001,
        1001,
        1001,
    ]
    assert "<b>最新资源</b>" in fake_transport.messages[0].text
    assert "<b>搜索结果</b>" in fake_transport.messages[1].text
    assert "<b>家业</b>" in fake_transport.messages[2].text


@pytest.mark.asyncio
async def test_create_merge_and_idempotent_replay_notifications(
    db_session: AsyncSession,
) -> None:
    fake_transport = FakeBotTransport()
    application = create_app(
        app_settings=_settings(notify_chat_ids="2001"),
        bot_transport=fake_transport,
        session_factory=_session_factory(db_session),
    )

    async with application.router.lifespan_context(application):
        event_bus = application.state.event_bus

        await event_bus.publish(
            RawMessageFailed(
                raw_message_id=999,
                parse_attempts=1,
                error="ignored",
            )
        )
        assert fake_transport.messages == []

        first = await _ingest_parse_dedup(
            db_session,
            event_bus=event_bus,
            raw_text=(
                "【家业】第1集 夸克 "
                "https://pan.quark.cn/s/p5e-create"
            ),
            tg_id=77101,
        )
        assert first.dedup_status == "new"
        assert len(fake_transport.messages) == 1
        assert "<b>新增资源</b>" in fake_transport.messages[0].text

        second = await _ingest_parse_dedup(
            db_session,
            event_bus=event_bus,
            raw_text=(
                "【家业】第1集 百度 "
                "https://pan.baidu.com/s/p5e-merge"
            ),
            tg_id=77102,
        )
        assert second.dedup_status == "matched"
        assert len(fake_transport.messages) == 2
        assert "<b>资源更新</b>" in fake_transport.messages[1].text

        replay = await RawMessageService(db_session).dedup_and_persist(
            second.id,
            event_bus=event_bus,
        )
        assert replay.dedup_status == "matched"
        assert len(fake_transport.messages) == 2


@pytest.mark.asyncio
async def test_notify_transport_failure_does_not_rollback_database(
    db_session: AsyncSession,
) -> None:
    fake_transport = FakeBotTransport(failing_chat_ids={2001})
    application = create_app(
        app_settings=_settings(notify_chat_ids="2001"),
        bot_transport=fake_transport,
        session_factory=_session_factory(db_session),
    )

    async with application.router.lifespan_context(application):
        result = await _ingest_parse_dedup(
            db_session,
            event_bus=application.state.event_bus,
            raw_text=(
                "【家业】第1集 夸克 "
                "https://pan.quark.cn/s/p5e-failure"
            ),
            tg_id=77201,
        )

        assert result.dedup_status == "new"
        assert fake_transport.attempted_chat_ids == [2001]
        assert fake_transport.messages == []
        assert await _count(db_session, Work) == 1
        assert await _count(db_session, Resource) == 1
        assert await _count(db_session, ResourceLink) == 1
        assert await _count(db_session, ResourceSource) == 1

"""P5-D tests using only an in-memory FakeBotTransport."""

from dataclasses import dataclass
from datetime import datetime, timezone
import re

import httpx
import pytest

from app.infra.eventbus import InMemoryEventBus
from app.infra.events import RawMessageFailed, ResourceCreated, ResourceMerged
from app.modules.bot.handlers import BotCommandHandler, ResourceNotifyHandler
from app.modules.bot.schema import TelegramUpdate
from app.modules.resource.query_schema import (
    LinkView,
    ResourceDetail,
    ResourceListItem,
    SourceView,
)

_NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SentMessage:
    chat_id: int
    text: str
    parse_mode: str


class FakeBotTransport:
    """Memory-only transport; it never imports or calls Telegram APIs."""

    def __init__(self, failing_chat_ids: set[int] | None = None) -> None:
        self.messages: list[SentMessage] = []
        self.attempted_chat_ids: list[int] = []
        self.failing_chat_ids = failing_chat_ids or set()

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


class FakeResourceQueryService:
    def __init__(
        self,
        *,
        latest_items: list[ResourceListItem] | None = None,
        search_items: list[ResourceListItem] | None = None,
        details: dict[int, ResourceDetail] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.latest_items = latest_items or []
        self.search_items = search_items or []
        self.details = details or {}
        self.failure = failure
        self.calls: list[tuple[object, ...]] = []

    async def latest(
        self,
        limit: int = 10,
        offset: int = 0,
    ) -> list[ResourceListItem]:
        self.calls.append(("latest", limit, offset))
        if self.failure:
            raise self.failure
        return self.latest_items

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
    ) -> list[ResourceListItem]:
        self.calls.append(("search", query, limit, offset))
        if self.failure:
            raise self.failure
        return self.search_items

    async def get_detail(self, resource_id: int) -> ResourceDetail | None:
        self.calls.append(("detail", resource_id))
        if self.failure:
            raise self.failure
        return self.details.get(resource_id)


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_real_http(*args, **kwargs):
        raise AssertionError("real HTTP is forbidden in P5-D tests")

    monkeypatch.setattr(httpx.AsyncClient, "post", fail_real_http)


@pytest.fixture
def list_item() -> ResourceListItem:
    return ResourceListItem(
        resource_id=11,
        work_id=7,
        title="家业",
        work_title="家业",
        content_type="drama",
        resource_type="single_episode",
        episode_no=1,
        season_no=1,
        episode_range="ep1",
        year=2026,
        quality="1080p",
        source_count=2,
        last_seen_at=_NOW,
    )


@pytest.fixture
def resource_detail() -> ResourceDetail:
    return ResourceDetail(
        resource_id=11,
        work_id=7,
        title="家业",
        work_title="家业",
        content_type="drama",
        resource_type="single_episode",
        episode_no=1,
        season_no=1,
        episode_range="ep1",
        year=2026,
        quality="1080p",
        source_count=2,
        last_seen_at=_NOW,
        title_norm="家业",
        work_title_norm="家业",
        description="剧情简介",
        tags=["drama"],
        first_seen_at=_NOW,
        links=[
            LinkView(
                link_id=1,
                provider="quark",
                url="https://pan.quark.cn/s/example",
                access_code="A123",
                password=None,
                link_type="url",
                status="active",
            )
        ],
        sources=[
            SourceView(
                source_id=1,
                raw_message_id=101,
                channel_id=5,
                channel_title="资源频道",
                channel_username="resource_channel",
                match_type="title_episode",
                confidence=0.95,
                matched_reason="resource_key hit",
                parser_version="0.2.0",
                rule_version="0.2.0",
                detected_at=_NOW,
            )
        ],
    )


def _update(text: str | None, chat_id: int = 1001) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=1,
        message_id=2,
        chat_id=chat_id,
        text=text,
    )


def _command_handler(
    query_service: FakeResourceQueryService,
    transport: FakeBotTransport,
    allowed_chat_ids: set[int] | None = None,
) -> BotCommandHandler:
    return BotCommandHandler(
        query_service=query_service,  # type: ignore[arg-type]
        transport=transport,
        allowed_chat_ids=(
            {1001} if allowed_chat_ids is None else allowed_chat_ids
        ),
    )


def test_telegram_update_extracts_only_required_scalars() -> None:
    update = TelegramUpdate.from_payload(
        {
            "update_id": 99,
            "message": {
                "message_id": 88,
                "chat": {
                    "id": 77,
                    "title": "must not be stored",
                    "type": "group",
                },
                "from": {"id": 66, "username": "must_not_be_stored"},
                "text": "/help",
                "entities": [{"type": "bot_command"}],
            },
            "callback_query": {"data": "ignored"},
        }
    )

    assert update.model_dump() == {
        "update_id": 99,
        "message_id": 88,
        "chat_id": 77,
        "text": "/help",
    }
    assert not hasattr(update, "user")
    assert not hasattr(update, "chat")
    assert not hasattr(update, "_sa_instance_state")


@pytest.mark.asyncio
async def test_latest_uses_default_limit_and_returns_html(
    list_item: ResourceListItem,
) -> None:
    query_service = FakeResourceQueryService(latest_items=[list_item])
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(_update("/latest"))

    assert query_service.calls == [("latest", 10, 0)]
    assert len(transport.messages) == 1
    assert transport.messages[0].chat_id == 1001
    assert transport.messages[0].parse_mode == "HTML"
    assert "<b>最新资源</b>" in transport.messages[0].text
    assert "家业" in transport.messages[0].text


@pytest.mark.asyncio
async def test_latest_accepts_limit_50(
    list_item: ResourceListItem,
) -> None:
    query_service = FakeResourceQueryService(latest_items=[list_item])
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(
        _update("/latest 50")
    )

    assert query_service.calls == [("latest", 50, 0)]
    assert len(transport.messages) == 1


@pytest.mark.parametrize("argument", ["not-an-int", "0", "51", "1 2"])
@pytest.mark.asyncio
async def test_latest_rejects_invalid_limit(argument: str) -> None:
    query_service = FakeResourceQueryService()
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(
        _update(f"/latest {argument}")
    )

    assert query_service.calls == []
    assert "<b>参数错误</b>" in transport.messages[0].text


@pytest.mark.asyncio
async def test_search_passes_query_and_returns_results(
    list_item: ResourceListItem,
) -> None:
    query_service = FakeResourceQueryService(search_items=[list_item])
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(
        _update("/search 家业")
    )

    assert query_service.calls == [("search", "家业", 10, 0)]
    assert "<b>搜索结果</b>" in transport.messages[0].text


@pytest.mark.asyncio
async def test_search_empty_argument_returns_parameter_error() -> None:
    query_service = FakeResourceQueryService()
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(
        _update("/search    ")
    )

    assert query_service.calls == []
    assert "<b>参数错误</b>" in transport.messages[0].text


@pytest.mark.asyncio
async def test_resource_returns_detail(
    resource_detail: ResourceDetail,
) -> None:
    query_service = FakeResourceQueryService(details={11: resource_detail})
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(
        _update("/resource 11")
    )

    assert query_service.calls == [("detail", 11)]
    assert "<b>家业</b>" in transport.messages[0].text
    assert "https://pan.quark.cn/s/example" in transport.messages[0].text


@pytest.mark.parametrize("argument", ["abc", "0", "-1", "1 2"])
@pytest.mark.asyncio
async def test_resource_rejects_invalid_id(argument: str) -> None:
    query_service = FakeResourceQueryService()
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(
        _update(f"/resource {argument}")
    )

    assert query_service.calls == []
    assert "<b>参数错误</b>" in transport.messages[0].text


@pytest.mark.asyncio
async def test_resource_missing_returns_not_found() -> None:
    query_service = FakeResourceQueryService()
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(
        _update("/resource 404")
    )

    assert query_service.calls == [("detail", 404)]
    assert "<b>未找到资源</b>" in transport.messages[0].text
    assert "<code>404</code>" in transport.messages[0].text


@pytest.mark.asyncio
async def test_help_returns_static_help() -> None:
    query_service = FakeResourceQueryService()
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(_update("/help"))

    assert query_service.calls == []
    assert "<b>可用命令</b>" in transport.messages[0].text
    assert "/latest [1-50]" in transport.messages[0].text


@pytest.mark.parametrize("allowed_chat_ids", [set(), {9999}])
@pytest.mark.asyncio
async def test_unauthorized_chat_is_silent(
    allowed_chat_ids: set[int],
) -> None:
    query_service = FakeResourceQueryService()
    transport = FakeBotTransport()

    await _command_handler(
        query_service,
        transport,
        allowed_chat_ids=allowed_chat_ids,
    ).handle(_update("/latest"))

    assert query_service.calls == []
    assert transport.messages == []
    assert transport.attempted_chat_ids == []


@pytest.mark.asyncio
async def test_query_failure_uses_safe_log_and_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_error = "SELECT * FROM secrets token=real-token"
    query_service = FakeResourceQueryService(
        failure=RuntimeError(secret_error)
    )
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(_update("/latest"))

    assert "<b>查询暂时不可用，请稍后重试。</b>" == (
        transport.messages[0].text
    )
    assert secret_error not in caplog.text
    assert "real-token" not in caplog.text
    assert "SELECT *" not in caplog.text


@pytest.mark.asyncio
async def test_detail_escapes_dynamic_html_and_uses_tag_allowlist() -> None:
    malicious = '<b>&"\''
    detail = ResourceDetail(
        resource_id=12,
        work_id=8,
        title=malicious,
        work_title=f"Work {malicious}",
        content_type=malicious,
        resource_type="single_episode",
        episode_no=1,
        season_no=1,
        episode_range=malicious,
        year=2026,
        quality=None,
        source_count=1,
        last_seen_at=_NOW,
        title_norm="malicious",
        work_title_norm="malicious",
        description=f"Description {malicious}",
        tags=[],
        first_seen_at=_NOW,
        links=[
            LinkView(
                link_id=1,
                provider=f"Provider {malicious}",
                url=(
                    "https://example.com/share?"
                    'value=<b>&quote="\''
                ),
                access_code=malicious,
                password=malicious,
                link_type="url",
                status="active",
            )
        ],
        sources=[
            SourceView(
                source_id=1,
                raw_message_id=1,
                channel_id=1,
                channel_title=f"Channel {malicious}",
                channel_username=f"user{malicious}",
                match_type="title_episode",
                confidence=1.0,
                matched_reason=f"Reason {malicious}",
                parser_version="0.2.0",
                rule_version="0.2.0",
                detected_at=_NOW,
            )
        ],
    )
    query_service = FakeResourceQueryService(details={12: detail})
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(
        _update("/resource 12")
    )

    text = transport.messages[0].text
    escaped = "&lt;b&gt;&amp;&quot;&#x27;"
    assert escaped in text
    assert "Provider " + escaped in text
    assert "Description " + escaped in text
    assert "Channel " + escaped in text
    assert "Reason " + escaped in text
    assert f"<code>{escaped}</code>" in text
    assert '<a href="https://example.com/share?' in text
    assert "value=&lt;b&gt;&amp;quote=&quot;&#x27;" in text

    tags = re.findall(r"</?[^>]+>", text)
    for tag in tags:
        assert (
            tag in {"<b>", "</b>", "<code>", "</code>", "</a>"}
            or re.fullmatch(r'<a href="[^"]*">', tag) is not None
        )

    assert not hasattr(detail, "_sa_instance_state")
    assert all(not hasattr(link, "_sa_instance_state") for link in detail.links)
    assert all(
        not hasattr(source, "_sa_instance_state") for source in detail.sources
    )


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "javascript:alert(1)",
        "data:text/html,<b>bad</b>",
        "ftp://example.com/file",
        "https:///missing-host",
    ],
)
@pytest.mark.asyncio
async def test_non_http_urls_are_plain_escaped_text(
    resource_detail: ResourceDetail,
    unsafe_url: str,
) -> None:
    unsafe_detail = resource_detail.model_copy(
        update={
            "links": [
                resource_detail.links[0].model_copy(
                    update={"url": unsafe_url}
                )
            ]
        }
    )
    query_service = FakeResourceQueryService(details={11: unsafe_detail})
    transport = FakeBotTransport()

    await _command_handler(query_service, transport).handle(
        _update("/resource 11")
    )

    text = transport.messages[0].text
    assert f'<a href="{unsafe_url}' not in text
    assert "javascript:" not in text or "<a href=" not in text
    if "<b>" in unsafe_url:
        assert "&lt;b&gt;" in text


@pytest.mark.asyncio
async def test_created_notification_queries_detail_and_formats_event(
    resource_detail: ResourceDetail,
) -> None:
    query_service = FakeResourceQueryService(details={11: resource_detail})
    transport = FakeBotTransport()
    handler = ResourceNotifyHandler(
        query_service=query_service,  # type: ignore[arg-type]
        transport=transport,
        notify_chat_ids=[2001],
    )
    event = ResourceCreated(
        resource_id=11,
        work_id=7,
        raw_message_id=101,
        source_count=3,
        occurred_at=_NOW,
    )

    await handler.handle_created(event)

    assert query_service.calls == [("detail", 11)]
    assert transport.messages[0].chat_id == 2001
    assert "<b>新增资源</b>" in transport.messages[0].text
    assert "<code>3</code>" in transport.messages[0].text
    assert _NOW.isoformat() in transport.messages[0].text


@pytest.mark.asyncio
async def test_merged_notification_has_distinct_heading(
    resource_detail: ResourceDetail,
) -> None:
    query_service = FakeResourceQueryService(details={11: resource_detail})
    transport = FakeBotTransport()
    handler = ResourceNotifyHandler(
        query_service=query_service,  # type: ignore[arg-type]
        transport=transport,
        notify_chat_ids=[2001],
    )
    event = ResourceMerged(
        resource_id=11,
        work_id=7,
        raw_message_id=102,
        source_count=4,
        created_link_count=1,
        created_source=True,
        occurred_at=_NOW,
    )

    await handler.handle_merged(event)

    assert "<b>资源更新</b>" in transport.messages[0].text
    assert "<code>4</code>" in transport.messages[0].text
    assert "<b>新增资源</b>" not in transport.messages[0].text


@pytest.mark.asyncio
async def test_notify_failure_does_not_block_later_chats(
    resource_detail: ResourceDetail,
) -> None:
    query_service = FakeResourceQueryService(details={11: resource_detail})
    transport = FakeBotTransport(failing_chat_ids={2002})
    handler = ResourceNotifyHandler(
        query_service=query_service,  # type: ignore[arg-type]
        transport=transport,
        notify_chat_ids=[2001, 2002, 2003],
    )
    event = ResourceCreated(
        resource_id=11,
        work_id=7,
        raw_message_id=101,
        source_count=2,
        occurred_at=_NOW,
    )

    await handler.handle_created(event)

    assert transport.attempted_chat_ids == [2001, 2002, 2003]
    assert [message.chat_id for message in transport.messages] == [2001, 2003]


@pytest.mark.asyncio
async def test_notify_missing_resource_logs_and_sends_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query_service = FakeResourceQueryService()
    transport = FakeBotTransport()
    handler = ResourceNotifyHandler(
        query_service=query_service,  # type: ignore[arg-type]
        transport=transport,
        notify_chat_ids=[2001],
    )
    event = ResourceCreated(
        resource_id=404,
        work_id=7,
        raw_message_id=101,
        source_count=1,
        occurred_at=_NOW,
    )

    await handler.handle_created(event)

    assert query_service.calls == [("detail", 404)]
    assert transport.messages == []
    assert "resource_id=404" in caplog.text


@pytest.mark.asyncio
async def test_raw_message_failed_is_not_connected_to_notify_handler(
    resource_detail: ResourceDetail,
) -> None:
    query_service = FakeResourceQueryService(details={11: resource_detail})
    transport = FakeBotTransport()
    handler = ResourceNotifyHandler(
        query_service=query_service,  # type: ignore[arg-type]
        transport=transport,
        notify_chat_ids=[2001],
    )
    event_bus = InMemoryEventBus()
    event_bus.subscribe(ResourceCreated, handler.handle_created)
    event_bus.subscribe(ResourceMerged, handler.handle_merged)

    await event_bus.publish(
        RawMessageFailed(
            raw_message_id=101,
            parse_attempts=1,
            error="parse failed",
            occurred_at=_NOW,
        )
    )

    assert query_service.calls == []
    assert transport.messages == []

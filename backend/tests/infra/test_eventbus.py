"""P5-A tests for scalar event contracts and InMemoryEventBus."""

from dataclasses import fields, is_dataclass
from datetime import datetime

import pytest

from app.infra.eventbus import InMemoryEventBus
from app.infra.events import (
    DomainEvent,
    RawMessageFailed,
    ResourceCreated,
    ResourceMerged,
)


def _resource_created() -> ResourceCreated:
    return ResourceCreated(
        resource_id=11,
        work_id=7,
        raw_message_id=101,
        source_count=1,
    )


@pytest.mark.asyncio
async def test_publish_calls_subscribed_handler() -> None:
    event_bus = InMemoryEventBus()
    received: list[DomainEvent] = []

    async def handler(event: DomainEvent) -> None:
        received.append(event)

    event_bus.subscribe(ResourceCreated, handler)
    event = _resource_created()

    await event_bus.publish(event)

    assert received == [event]


@pytest.mark.asyncio
async def test_publish_calls_multiple_handlers() -> None:
    event_bus = InMemoryEventBus()
    calls: list[str] = []

    async def first_handler(event: DomainEvent) -> None:
        calls.append("first")

    async def second_handler(event: DomainEvent) -> None:
        calls.append("second")

    event_bus.subscribe(ResourceCreated, first_handler)
    event_bus.subscribe(ResourceCreated, second_handler)

    await event_bus.publish(_resource_created())

    assert set(calls) == {"first", "second"}


@pytest.mark.asyncio
async def test_handlers_run_in_registration_order() -> None:
    event_bus = InMemoryEventBus()
    calls: list[int] = []

    async def first_handler(event: DomainEvent) -> None:
        calls.append(1)

    async def second_handler(event: DomainEvent) -> None:
        calls.append(2)

    async def third_handler(event: DomainEvent) -> None:
        calls.append(3)

    event_bus.subscribe(ResourceCreated, first_handler)
    event_bus.subscribe(ResourceCreated, second_handler)
    event_bus.subscribe(ResourceCreated, third_handler)

    await event_bus.publish(_resource_created())

    assert calls == [1, 2, 3]


@pytest.mark.asyncio
async def test_failing_handler_does_not_block_later_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event_bus = InMemoryEventBus()
    calls: list[str] = []

    async def failing_handler(event: DomainEvent) -> None:
        calls.append("failing")
        raise RuntimeError("consumer failed")

    async def later_handler(event: DomainEvent) -> None:
        calls.append("later")

    event_bus.subscribe(ResourceCreated, failing_handler)
    event_bus.subscribe(ResourceCreated, later_handler)

    await event_bus.publish(_resource_created())

    assert calls == ["failing", "later"]
    assert "event_bus.handler_failed" in caplog.text
    assert "consumer failed" not in caplog.text


@pytest.mark.asyncio
async def test_publish_does_not_raise_handler_exception() -> None:
    event_bus = InMemoryEventBus()

    async def failing_handler(event: DomainEvent) -> None:
        raise ValueError("hidden from publisher")

    event_bus.subscribe(ResourceCreated, failing_handler)

    await event_bus.publish(_resource_created())


@pytest.mark.asyncio
async def test_publish_without_subscribers_returns_safely() -> None:
    event_bus = InMemoryEventBus()

    result = await event_bus.publish(_resource_created())

    assert result is None


@pytest.mark.asyncio
async def test_handlers_are_isolated_by_exact_event_type() -> None:
    event_bus = InMemoryEventBus()
    received: list[DomainEvent] = []

    async def handler(event: DomainEvent) -> None:
        received.append(event)

    event_bus.subscribe(ResourceCreated, handler)

    await event_bus.publish(
        RawMessageFailed(
            raw_message_id=101,
            parse_attempts=1,
            error="no valid parser result",
        )
    )

    assert received == []


def test_event_contracts_contain_only_scalars_and_no_orm_state() -> None:
    events = (
        _resource_created(),
        ResourceMerged(
            resource_id=11,
            work_id=7,
            raw_message_id=102,
            source_count=2,
            created_link_count=1,
            created_source=True,
        ),
        RawMessageFailed(
            raw_message_id=103,
            parse_attempts=2,
            error="no valid parser result",
        ),
    )

    for event in events:
        assert is_dataclass(event)
        assert not hasattr(event, "_sa_instance_state")
        assert event.occurred_at.tzinfo is not None
        assert all(
            isinstance(getattr(event, item.name), (bool, int, str, datetime))
            for item in fields(event)
        )

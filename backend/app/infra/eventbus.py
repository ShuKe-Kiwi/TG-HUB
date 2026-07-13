"""In-process domain event bus."""

from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from app.infra.events import DomainEvent
from app.infra.logger import get_logger

logger = get_logger(__name__)

EventT = TypeVar("EventT", bound=DomainEvent)
EventHandler = Callable[[EventT], Awaitable[None]]


class EventBus(ABC):
    """Interface for subscribing to and publishing domain events."""

    @abstractmethod
    def subscribe(
        self,
        event_type: type[EventT],
        handler: EventHandler[EventT],
    ) -> None:
        """Register an async handler for an exact event type."""

    @abstractmethod
    async def publish(self, event: DomainEvent) -> None:
        """Publish an event without exposing consumer failures."""


class InMemoryEventBus(EventBus):
    """Sequential, best-effort event delivery within the current process."""

    def __init__(self) -> None:
        self._handlers: dict[
            type[DomainEvent],
            list[EventHandler[DomainEvent]],
        ] = defaultdict(list)

    def subscribe(
        self,
        event_type: type[EventT],
        handler: EventHandler[EventT],
    ) -> None:
        """Append a handler, preserving registration order."""
        self._handlers[event_type].append(
            cast(EventHandler[DomainEvent], handler)
        )

    async def publish(self, event: DomainEvent) -> None:
        """Await every matching handler and isolate handler exceptions."""
        handlers = tuple(self._handlers.get(type(event), ()))
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.error(
                    "event_bus.handler_failed",
                    extra={"error_code": "EVENT_HANDLER_FAILED", "recoverable": True},
                )

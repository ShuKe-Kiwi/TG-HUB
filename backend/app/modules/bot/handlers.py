"""Telegram command and resource notification handlers."""

from collections.abc import Collection
from datetime import datetime

from app.infra.events import ResourceCreated, ResourceMerged
from app.infra.logger import get_logger
from app.modules.bot import formatter
from app.modules.bot.schema import TelegramUpdate
from app.modules.bot.transport import BotTransport
from app.modules.resource.query_service import ResourceQueryService

logger = get_logger(__name__)


class BotCommandHandler:
    def __init__(
        self,
        query_service: ResourceQueryService,
        transport: BotTransport,
        allowed_chat_ids: Collection[int],
    ) -> None:
        self._query_service = query_service
        self._transport = transport
        self._allowed_chat_ids = frozenset(allowed_chat_ids)

    async def handle(self, update: TelegramUpdate) -> None:
        if update.chat_id not in self._allowed_chat_ids:
            return
        if not update.text:
            return

        command_token, _, raw_argument = update.text.strip().partition(" ")
        command = command_token.split("@", 1)[0].lower()
        argument = raw_argument.strip()

        try:
            if command == "/latest":
                await self._handle_latest(update.chat_id, argument)
            elif command == "/search":
                await self._handle_search(update.chat_id, argument)
            elif command == "/resource":
                await self._handle_resource(update.chat_id, argument)
            elif command == "/help":
                await self._send(update.chat_id, formatter.format_help())
            else:
                await self._send(
                    update.chat_id,
                    formatter.format_unknown_command(),
                )
        except Exception:
            logger.error(
                "Bot query failed command=%s chat_id=%s",
                command,
                update.chat_id,
            )
            await self._send(
                update.chat_id,
                formatter.format_query_error(),
            )

    async def _handle_latest(self, chat_id: int, argument: str) -> None:
        if argument:
            try:
                limit = int(argument)
            except ValueError:
                await self._send(
                    chat_id,
                    formatter.format_parameter_error("latest"),
                )
                return
            if not 1 <= limit <= 50:
                await self._send(
                    chat_id,
                    formatter.format_parameter_error("latest"),
                )
                return
        else:
            limit = 10

        items = await self._query_service.latest(limit=limit)
        await self._send(chat_id, formatter.format_latest(items))

    async def _handle_search(self, chat_id: int, argument: str) -> None:
        if not argument:
            await self._send(
                chat_id,
                formatter.format_parameter_error("search"),
            )
            return
        items = await self._query_service.search(argument)
        await self._send(
            chat_id,
            formatter.format_search_results(argument, items),
        )

    async def _handle_resource(self, chat_id: int, argument: str) -> None:
        try:
            resource_id = int(argument)
        except ValueError:
            await self._send(
                chat_id,
                formatter.format_parameter_error("resource"),
            )
            return
        if resource_id <= 0:
            await self._send(
                chat_id,
                formatter.format_parameter_error("resource"),
            )
            return

        detail = await self._query_service.get_detail(resource_id)
        if detail is None:
            await self._send(
                chat_id,
                formatter.format_resource_not_found(resource_id),
            )
            return
        await self._send(
            chat_id,
            formatter.format_resource_detail(detail),
        )

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            await self._transport.send_message(
                chat_id,
                text,
                parse_mode="HTML",
            )
        except Exception:
            logger.error("Bot transport failed chat_id=%s", chat_id)


class ResourceNotifyHandler:
    def __init__(
        self,
        query_service: ResourceQueryService,
        transport: BotTransport,
        notify_chat_ids: Collection[int],
    ) -> None:
        self._query_service = query_service
        self._transport = transport
        self._notify_chat_ids = tuple(dict.fromkeys(notify_chat_ids))

    async def handle_created(self, event: ResourceCreated) -> None:
        await self._handle(
            resource_id=event.resource_id,
            source_count=event.source_count,
            occurred_at=event.occurred_at,
            merged=False,
        )

    async def handle_merged(self, event: ResourceMerged) -> None:
        await self._handle(
            resource_id=event.resource_id,
            source_count=event.source_count,
            occurred_at=event.occurred_at,
            merged=True,
        )

    async def _handle(
        self,
        *,
        resource_id: int,
        source_count: int,
        occurred_at: datetime,
        merged: bool,
    ) -> None:
        if not self._notify_chat_ids:
            return

        try:
            detail = await self._query_service.get_detail(resource_id)
        except Exception:
            logger.error(
                "Resource notification query failed resource_id=%s",
                resource_id,
            )
            return
        if detail is None:
            logger.warning(
                "Resource notification skipped missing resource_id=%s",
                resource_id,
            )
            return

        if merged:
            text = formatter.format_resource_merged_notification(
                detail,
                source_count,
                occurred_at,
            )
        else:
            text = formatter.format_resource_created_notification(
                detail,
                source_count,
                occurred_at,
            )

        for chat_id in self._notify_chat_ids:
            try:
                await self._transport.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML",
                )
            except Exception:
                logger.error(
                    "Resource notification transport failed chat_id=%s "
                    "resource_id=%s",
                    chat_id,
                    resource_id,
                )

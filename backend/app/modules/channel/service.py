"""Channel service — create_or_get idempotent semantics."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.logger import get_logger
from app.modules.channel.model import Channel
from app.modules.channel.repository import ChannelRepository
from app.modules.channel.schema import ChannelCreate

logger = get_logger(__name__)


class ChannelService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ChannelRepository(session)

    async def create_or_get(self, data: ChannelCreate) -> Channel:
        """Idempotent create: if tg_id exists, return existing without modification."""
        existing = await self.repo.get_by_tg_id(data.tg_id)
        if existing is not None:
            logger.info("channel.duplicate")
            return existing

        channel = Channel(
            name=data.name,
            tg_id=data.tg_id,
            tg_username=data.tg_username,
            source_type=data.source_type,
            status=data.status,
            rule_profile=data.rule_profile,
            special_parser=data.special_parser,
        )
        await self.repo.create(channel)
        await self.session.commit()
        logger.info("channel.created")
        return channel

    async def get_by_id(self, channel_id: int) -> Channel | None:
        return await self.repo.get_by_id(channel_id)

    async def list_all(self) -> list[Channel]:
        return await self.repo.list_all()

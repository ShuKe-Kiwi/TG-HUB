"""Channel repository — minimal data access."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.channel.model import Channel


class ChannelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_tg_id(self, tg_id: int) -> Channel | None:
        stmt = select(Channel).where(Channel.tg_id == tg_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, channel_id: int) -> Channel | None:
        stmt = select(Channel).where(Channel.id == channel_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, channel: Channel) -> Channel:
        self.session.add(channel)
        await self.session.flush()
        await self.session.refresh(channel)
        return channel

    async def list_all(self) -> list[Channel]:
        stmt = select(Channel).order_by(Channel.id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

"""RawMessage repository — minimal data access."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.rawmessage.model import RawMessage


class RawMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_channel_and_msg_id(
        self, channel_id: int, tg_message_id: int
    ) -> RawMessage | None:
        stmt = select(RawMessage).where(
            RawMessage.channel_id == channel_id,
            RawMessage.tg_message_id == tg_message_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, raw_msg_id: int) -> RawMessage | None:
        stmt = select(RawMessage).where(RawMessage.id == raw_msg_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, raw_message: RawMessage) -> RawMessage:
        self.session.add(raw_message)
        await self.session.flush()
        await self.session.refresh(raw_message)
        return raw_message

"""Minimal persistence operations for the Resource Registry.

P4-B additions:
- get_or_create methods on ResourceLinkRepository and ResourceSourceRepository
- count_sources on ResourceSourceRepository
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.resource.model import (
    Resource,
    ResourceLink,
    ResourceSource,
    Work,
)


class WorkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, work_id: int) -> Work | None:
        return await self.session.get(Work, work_id)

    async def get_by_work_key(self, work_key: str) -> Work | None:
        result = await self.session.execute(
            select(Work).where(Work.work_key == work_key)
        )
        return result.scalar_one_or_none()

    async def create(self, work: Work) -> Work:
        self.session.add(work)
        await self.session.flush()
        await self.session.refresh(work)
        return work


class ResourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, resource_id: int) -> Resource | None:
        return await self.session.get(Resource, resource_id)

    async def get_by_resource_key(
        self,
        resource_key: str,
    ) -> Resource | None:
        result = await self.session.execute(
            select(Resource).where(Resource.resource_key == resource_key)
        )
        return result.scalar_one_or_none()

    async def create(self, resource: Resource) -> Resource:
        self.session.add(resource)
        await self.session.flush()
        await self.session.refresh(resource)
        return resource


class ResourceLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, link_id: int) -> ResourceLink | None:
        return await self.session.get(ResourceLink, link_id)

    async def get_by_identity(
        self,
        resource_id: int,
        provider: str,
        url_hash: str,
    ) -> ResourceLink | None:
        result = await self.session.execute(
            select(ResourceLink).where(
                ResourceLink.resource_id == resource_id,
                ResourceLink.provider == provider,
                ResourceLink.url_hash == url_hash,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, link: ResourceLink) -> ResourceLink:
        self.session.add(link)
        await self.session.flush()
        await self.session.refresh(link)
        return link

    async def get_or_create(
        self,
        resource_id: int,
        provider: str,
        url_hash: str,
        **fields: object,
    ) -> tuple[ResourceLink, bool]:
        """SELECT-first get_or_create.

        Returns (existing_link, False) if (resource_id, provider, url_hash) exists,
        otherwise (new_link, True).

        Uses the unique constraint (resource_id, provider, url_hash) as the
        identity check — no IntegrityError risk.
        """
        existing = await self.get_by_identity(resource_id, provider, url_hash)
        if existing is not None:
            return existing, False

        link = ResourceLink(
            resource_id=resource_id,
            provider=provider,
            url_hash=url_hash,
            **fields,
        )
        self.session.add(link)
        await self.session.flush()
        await self.session.refresh(link)
        return link, True


class ResourceSourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, source_id: int) -> ResourceSource | None:
        return await self.session.get(ResourceSource, source_id)

    async def get_by_identity(
        self,
        resource_id: int,
        raw_message_id: int,
    ) -> ResourceSource | None:
        result = await self.session.execute(
            select(ResourceSource).where(
                ResourceSource.resource_id == resource_id,
                ResourceSource.raw_message_id == raw_message_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, source: ResourceSource) -> ResourceSource:
        self.session.add(source)
        await self.session.flush()
        await self.session.refresh(source)
        return source

    async def get_or_create(
        self,
        resource_id: int,
        raw_message_id: int,
        **fields: object,
    ) -> tuple[ResourceSource, bool]:
        """SELECT-first get_or_create for ResourceSource.

        Returns (existing_source, False) if (resource_id, raw_message_id) exists,
        otherwise (new_source, True).

        Never raises IntegrityError — safe for repeated calls.
        """
        existing = await self.get_by_identity(resource_id, raw_message_id)
        if existing is not None:
            return existing, False

        source = ResourceSource(
            resource_id=resource_id,
            raw_message_id=raw_message_id,
            **fields,
        )
        self.session.add(source)
        await self.session.flush()
        await self.session.refresh(source)
        return source, True

    async def count_by_resource(self, resource_id: int) -> int:
        """Actual COUNT of ResourceSource records for a given Resource."""
        result = await self.session.execute(
            select(func.count(ResourceSource.id)).where(
                ResourceSource.resource_id == resource_id,
            )
        )
        count: int = result.scalar_one()
        return count

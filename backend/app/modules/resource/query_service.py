"""Read-only Resource projections for presentation adapters."""

from sqlalchemy import or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.channel.model import Channel
from app.modules.resource.model import (
    Resource,
    ResourceLink,
    ResourceSource,
    Work,
)
from app.modules.resource.query_schema import (
    LinkView,
    ResourceDetail,
    ResourceListItem,
    SourceView,
)

_MAX_LIMIT = 50
_LIKE_ESCAPE = "\\"

_LIST_COLUMNS = (
    Resource.id.label("resource_id"),
    Resource.work_id.label("work_id"),
    Resource.title.label("title"),
    Work.title.label("work_title"),
    Work.type.label("content_type"),
    Resource.resource_type.label("resource_type"),
    Resource.episode_no.label("episode_no"),
    Resource.season_no.label("season_no"),
    Resource.episode_range.label("episode_range"),
    Resource.year.label("year"),
    Resource.quality.label("quality"),
    Resource.source_count.label("source_count"),
    Resource.last_seen_at.label("last_seen_at"),
)


def _validate_pagination(limit: int, offset: int) -> None:
    if not 1 <= limit <= _MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be greater than or equal to 0")


def _escape_like(value: str) -> str:
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


def _to_list_item(row: RowMapping) -> ResourceListItem:
    return ResourceListItem(
        resource_id=row["resource_id"],
        work_id=row["work_id"],
        title=row["title"],
        work_title=row["work_title"],
        content_type=row["content_type"],
        resource_type=row["resource_type"],
        episode_no=row["episode_no"],
        season_no=row["season_no"],
        episode_range=row["episode_range"],
        year=row["year"],
        quality=row["quality"],
        source_count=row["source_count"],
        last_seen_at=row["last_seen_at"],
    )


class ResourceQueryService:
    """Build presentation DTOs using SELECT-only scalar projections."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def latest(
        self,
        limit: int = 10,
        offset: int = 0,
    ) -> list[ResourceListItem]:
        _validate_pagination(limit, offset)
        statement = (
            select(*_LIST_COLUMNS)
            .join(Work, Work.id == Resource.work_id)
            .where(
                Work.status == "active",
                Resource.status == "active",
            )
            .order_by(
                Resource.last_seen_at.desc(),
                Resource.id.desc(),
            )
            .limit(limit)
            .offset(offset)
            .execution_options(autoflush=False)
        )
        rows = (await self.session.execute(statement)).mappings().all()
        return [_to_list_item(row) for row in rows]

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
    ) -> list[ResourceListItem]:
        _validate_pagination(limit, offset)
        normalized_query = query.strip()
        if not normalized_query:
            return []

        escaped_query = _escape_like(normalized_query)
        pattern = f"%{escaped_query}%"
        statement = (
            select(*_LIST_COLUMNS)
            .join(Work, Work.id == Resource.work_id)
            .where(
                Work.status == "active",
                Resource.status == "active",
                or_(
                    Resource.title.ilike(pattern, escape=_LIKE_ESCAPE),
                    Resource.title_norm.ilike(pattern, escape=_LIKE_ESCAPE),
                    Work.title.ilike(pattern, escape=_LIKE_ESCAPE),
                    Work.title_norm.ilike(pattern, escape=_LIKE_ESCAPE),
                ),
            )
            .order_by(
                Resource.last_seen_at.desc(),
                Resource.id.desc(),
            )
            .limit(limit)
            .offset(offset)
            .execution_options(autoflush=False)
        )
        rows = (await self.session.execute(statement)).mappings().all()
        return [_to_list_item(row) for row in rows]

    async def get_detail(self, resource_id: int) -> ResourceDetail | None:
        resource_statement = (
            select(
                *_LIST_COLUMNS,
                Resource.title_norm.label("title_norm"),
                Work.title_norm.label("work_title_norm"),
                Resource.description.label("description"),
                Resource.tags.label("tags"),
                Resource.first_seen_at.label("first_seen_at"),
            )
            .join(Work, Work.id == Resource.work_id)
            .where(
                Resource.id == resource_id,
                Work.status == "active",
                Resource.status == "active",
            )
            .execution_options(autoflush=False)
        )
        resource_row = (
            await self.session.execute(resource_statement)
        ).mappings().one_or_none()
        if resource_row is None:
            return None

        link_statement = (
            select(
                ResourceLink.id.label("link_id"),
                ResourceLink.provider.label("provider"),
                ResourceLink.normalized_url.label("normalized_url"),
                ResourceLink.original_url.label("original_url"),
                ResourceLink.access_code.label("access_code"),
                ResourceLink.password.label("password"),
                ResourceLink.link_type.label("link_type"),
                ResourceLink.status.label("status"),
            )
            .where(ResourceLink.resource_id == resource_id)
            .order_by(ResourceLink.provider.asc(), ResourceLink.id.asc())
            .execution_options(autoflush=False)
        )
        link_rows = (await self.session.execute(link_statement)).mappings().all()
        links = [
            LinkView(
                link_id=row["link_id"],
                provider=row["provider"],
                url=row["normalized_url"] or row["original_url"],
                access_code=row["access_code"],
                password=row["password"],
                link_type=row["link_type"],
                status=row["status"],
            )
            for row in link_rows
        ]

        source_statement = (
            select(
                ResourceSource.id.label("source_id"),
                ResourceSource.raw_message_id.label("raw_message_id"),
                ResourceSource.channel_id.label("channel_id"),
                Channel.name.label("channel_title"),
                Channel.tg_username.label("channel_username"),
                ResourceSource.match_type.label("match_type"),
                ResourceSource.confidence.label("confidence"),
                ResourceSource.matched_reason.label("matched_reason"),
                ResourceSource.parser_version.label("parser_version"),
                ResourceSource.rule_version.label("rule_version"),
                ResourceSource.detected_at.label("detected_at"),
            )
            .outerjoin(Channel, Channel.id == ResourceSource.channel_id)
            .where(ResourceSource.resource_id == resource_id)
            .order_by(
                ResourceSource.detected_at.desc(),
                ResourceSource.id.desc(),
            )
            .execution_options(autoflush=False)
        )
        source_rows = (
            await self.session.execute(source_statement)
        ).mappings().all()
        sources = [
            SourceView(
                source_id=row["source_id"],
                raw_message_id=row["raw_message_id"],
                channel_id=row["channel_id"],
                channel_title=row["channel_title"],
                channel_username=row["channel_username"],
                match_type=row["match_type"],
                confidence=row["confidence"],
                matched_reason=row["matched_reason"],
                parser_version=row["parser_version"],
                rule_version=row["rule_version"],
                detected_at=row["detected_at"],
            )
            for row in source_rows
        ]

        return ResourceDetail(
            resource_id=resource_row["resource_id"],
            work_id=resource_row["work_id"],
            title=resource_row["title"],
            work_title=resource_row["work_title"],
            content_type=resource_row["content_type"],
            resource_type=resource_row["resource_type"],
            episode_no=resource_row["episode_no"],
            season_no=resource_row["season_no"],
            episode_range=resource_row["episode_range"],
            year=resource_row["year"],
            quality=resource_row["quality"],
            source_count=resource_row["source_count"],
            last_seen_at=resource_row["last_seen_at"],
            title_norm=resource_row["title_norm"],
            work_title_norm=resource_row["work_title_norm"],
            description=resource_row["description"],
            tags=list(resource_row["tags"]),
            first_seen_at=resource_row["first_seen_at"],
            links=links,
            sources=sources,
        )

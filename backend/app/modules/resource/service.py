"""DedupService — Resource Registry dedup and merge logic.

Architecture alignment:
  Work.type       = normalized.content_type      (drama / movie / variety / anime / other)
  Resource.resource_type = normalized.episode_kind (single_episode / episode_range / full / unknown)

P4-B boundary:
  - Pure service: takes NormalizedResource + ParsedResource + ids as input
  - NOT connected to ParserPipeline or RawMessage batch processing
  - NOT using EventBus / Bot / Transfer
  - NOT doing fuzzy matching or AI
"""

import hashlib
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.normalizer.core import NormalizedResource
from app.modules.parser.dto import (
    LinkProvider,
    ParsedLink,
    ParsedMetadata,
    ParsedResource,
)
from app.modules.resource.model import (
    Resource,
    ResourceLink,
    ResourceSource,
    Work,
)
from app.modules.resource.repository import (
    ResourceLinkRepository,
    ResourceRepository,
    ResourceSourceRepository,
    WorkRepository,
)
from app.modules.resource.schema import DedupResult

# Default match_type label for P4-B — resource_key-based exact dedup.
_MATCH_TYPE = "title_episode"


# ---------------------------------------------------------------------------
# Serialization helper (per user requirement: not .model_dump())
# ---------------------------------------------------------------------------
def serialize_parsed_resource(parsed: ParsedResource) -> dict[str, Any]:
    """Serialize a ParsedResource to a JSON-safe dict.

    Handles nested ParsedLink (with LinkProvider enum → str),
    ParsedMetadata, and list fields.  Safe for JSONB storage.
    """
    return {
        "title": parsed.title,
        "raw_title": parsed.raw_title,
        "description": parsed.description,
        "resource_type": parsed.resource_type,
        "links": [_serialize_link(lnk) for lnk in parsed.links],
        "metadata": _serialize_metadata(parsed.metadata),
        "tags": list(parsed.tags),
        "confidence": parsed.confidence,
        "parser_version": parsed.parser_version,
        "rule_version": parsed.rule_version,
    }


def _serialize_link(link: ParsedLink) -> dict[str, Any]:
    return {
        "provider": link.provider.value
        if isinstance(link.provider, LinkProvider)
        else str(link.provider),
        "original_text": link.original_text,
        "url": link.url,
        "share_id": link.share_id,
        "access_code": link.access_code,
        "password": link.password,
        "link_type": link.link_type,
        "confidence": link.confidence,
    }


def _serialize_metadata(
    metadata: ParsedMetadata | None,
) -> dict[str, Any] | None:
    if metadata is None:
        return None
    return {
        "episode_no": metadata.episode_no,
        "season_no": metadata.season_no,
        "episode_range": metadata.episode_range,
        "year": metadata.year,
        "quality": metadata.quality,
        "file_size": metadata.file_size,
        "language": metadata.language,
        "subtitle": metadata.subtitle,
    }


# ---------------------------------------------------------------------------
# URL hash builder (stable, deterministic)
# ---------------------------------------------------------------------------
def _build_url_hash(link: ParsedLink) -> str:
    """Stable sha256 hexdigest for ResourceLink dedup.

    Priority:
      1. url.strip() if present
      2. password.strip() if present (xunlei command)
      3. original_text.strip() otherwise (magnet / text_code)
    """
    if link.url:
        raw = link.url.strip()
    elif link.password:
        raw = link.password.strip()
    else:
        raw = link.original_text.strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_provider_str(
    provider: LinkProvider | str,
) -> str:
    """Normalize provider to a plain string for DB storage."""
    if isinstance(provider, LinkProvider):
        return provider.value
    return str(provider)


# ---------------------------------------------------------------------------
# DedupService
# ---------------------------------------------------------------------------
class DedupService:
    """Resource Registry dedup and merge service.

    One service call = one transaction (flush driven here, commit owned by
    caller).  Idempotent under all unique constraints.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.work_repo = WorkRepository(session)
        self.resource_repo = ResourceRepository(session)
        self.link_repo = ResourceLinkRepository(session)
        self.source_repo = ResourceSourceRepository(session)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    async def dedup(
        self,
        normalized: NormalizedResource,
        parsed: ParsedResource,
        raw_message_id: int,
        channel_id: int,
    ) -> DedupResult:
        """Run dedup/merge for a parsed resource.

        Transaction boundary: all Repository operations share ``self.session``.
        The caller is responsible for the final commit/rollback.
        """
        resource_key = normalized.resource_key
        existing = await self.resource_repo.get_by_resource_key(resource_key)

        if existing is not None:
            return await self._merge(
                existing=existing,
                normalized=normalized,
                parsed=parsed,
                raw_message_id=raw_message_id,
                channel_id=channel_id,
            )
        return await self._create(
            normalized=normalized,
            parsed=parsed,
            raw_message_id=raw_message_id,
            channel_id=channel_id,
        )

    # ------------------------------------------------------------------
    # CREATE PATH: resource_key miss — new Resource
    # ------------------------------------------------------------------
    async def _create(
        self,
        normalized: NormalizedResource,
        parsed: ParsedResource,
        raw_message_id: int,
        channel_id: int,
    ) -> DedupResult:
        # --- Work: get or create ---
        work = await self.work_repo.get_by_work_key(normalized.work_key)
        if work is None:
            work = Work(
                title=normalized.title_norm,
                title_norm=normalized.title_norm,
                type=str(normalized.content_type),  # ← content_type, not episode_kind
                aliases=[],
                year=normalized.year,
                work_key=normalized.work_key,
            )
            work = await self.work_repo.create(work)

        # --- Resource: always new here ---
        now = datetime.now(timezone.utc)
        resource = Resource(
            work_id=work.id,
            title=parsed.title,
            title_norm=normalized.title_norm,
            resource_type=normalized.episode_kind,  # ← episode_kind, not content_type
            episode_no=normalized.episode_no,
            season_no=normalized.season_no,
            episode_range=normalized.episode_range,
            year=normalized.year,
            quality=(
                parsed.metadata.quality
                if parsed.metadata is not None
                else None
            ),
            resource_key=normalized.resource_key,
            episode_key=normalized.episode_key,
            content_fingerprint=normalized.content_fingerprint,
            description=parsed.description,
            tags=list(parsed.tags),
            source_count=0,  # will be set to actual count below
            first_seen_at=now,
            last_seen_at=now,
        )
        resource = await self.resource_repo.create(resource)

        # --- Links: get_or_create (idempotent even for create path) ---
        created_link_count = 0
        for link in parsed.links:
            _, was_created = await self.link_repo.get_or_create(
                resource_id=resource.id,
                provider=_ensure_provider_str(link.provider),
                url_hash=_build_url_hash(link),
                original_text=link.original_text,
                original_url=link.url,
                normalized_url=link.url.strip() if link.url else None,
                share_id=link.share_id,
                access_code=link.access_code,
                password=link.password,
                link_type=link.link_type,
            )
            if was_created:
                created_link_count += 1

        # --- ResourceSource: get_or_create ---
        snapshot = serialize_parsed_resource(parsed)
        _, created_source = await self.source_repo.get_or_create(
            resource_id=resource.id,
            raw_message_id=raw_message_id,
            channel_id=channel_id,
            match_type=_MATCH_TYPE,
            confidence=parsed.confidence,
            matched_reason=f"resource_key miss: {normalized.resource_key}",
            parsed_snapshot=snapshot,
            parser_version=parsed.parser_version,
            rule_version=parsed.rule_version,
        )

        # --- source_count: actual COUNT, not blind +1 ---
        actual_count = await self.source_repo.count_by_resource(resource.id)
        resource.source_count = actual_count
        await self.session.flush()

        return DedupResult(
            resource_id=resource.id,
            work_id=work.id,
            is_new=True,
            matched_reason=f"resource_key miss: {normalized.resource_key}",
            source_count=actual_count,
            created_link_count=created_link_count,
            created_source=created_source,
        )

    # ------------------------------------------------------------------
    # MERGE PATH: resource_key hit — merge into existing Resource
    # ------------------------------------------------------------------
    async def _merge(
        self,
        existing: Resource,
        normalized: NormalizedResource,
        parsed: ParsedResource,
        raw_message_id: int,
        channel_id: int,
    ) -> DedupResult:
        # --- Links: get_or_create ---
        created_link_count = 0
        for link in parsed.links:
            _, was_created = await self.link_repo.get_or_create(
                resource_id=existing.id,
                provider=_ensure_provider_str(link.provider),
                url_hash=_build_url_hash(link),
                original_text=link.original_text,
                original_url=link.url,
                normalized_url=link.url.strip() if link.url else None,
                share_id=link.share_id,
                access_code=link.access_code,
                password=link.password,
                link_type=link.link_type,
            )
            if was_created:
                created_link_count += 1

        # --- ResourceSource: get_or_create ---
        snapshot = serialize_parsed_resource(parsed)
        source, created_source = await self.source_repo.get_or_create(
            resource_id=existing.id,
            raw_message_id=raw_message_id,
            channel_id=channel_id,
            match_type=_MATCH_TYPE,
            confidence=parsed.confidence,
            matched_reason=f"resource_key hit: {normalized.resource_key}",
            parsed_snapshot=snapshot,
            parser_version=parsed.parser_version,
            rule_version=parsed.rule_version,
        )

        # --- source_count: actual COUNT, not blind +1 ---
        actual_count = await self.source_repo.count_by_resource(existing.id)
        existing.source_count = actual_count
        existing.last_seen_at = datetime.now(timezone.utc)
        await self.session.flush()

        return DedupResult(
            resource_id=existing.id,
            work_id=existing.work_id,
            is_new=False,
            matched_reason=f"resource_key hit: {normalized.resource_key}",
            source_count=actual_count,
            created_link_count=created_link_count,
            created_source=created_source,
        )

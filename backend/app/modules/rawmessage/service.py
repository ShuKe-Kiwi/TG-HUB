"""RawMessage service — idempotent ingest per ARCHITECTURE.md V2.1-final §2.2.

Rules:
1. If (channel_id, tg_message_id) already exists → return existing, no modification.
2. If new → compute content_hash = sha256(raw_text or ""), create with initial states.
3. content_hash is NOT a unique constraint — it's a regular index for backup dedup only.
4. raw_text / raw_payload / raw_media_refs must be saved verbatim.
5. raw_media_refs stores references only — no media download.
"""

import hashlib
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.eventbus import EventBus
from app.infra.events import ResourceCreated, ResourceMerged
from app.infra.logger import get_logger
from app.modules.normalizer.core import normalize_resource
from app.modules.parser.pipeline.core import (
    PARSER_VERSION,
    RULE_VERSION,
    ParserPipeline,
)
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.repository import RawMessageRepository
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.resource.schema import DedupResult
from app.modules.resource.service import DedupService, deserialize_parsed_resource

logger = get_logger(__name__)

EMPTY_PARSE_ERROR = "ParserPipeline returned no resources"


def _compute_content_hash(raw_text: str) -> str:
    """sha256(raw_text or "") — always returns a hash, even for empty text."""
    return hashlib.sha256((raw_text or "").encode("utf-8")).hexdigest()


class RawMessageService:
    def __init__(
        self,
        session: AsyncSession,
        parser_pipeline: ParserPipeline | None = None,
        dedup_service: DedupService | None = None,
    ) -> None:
        self.session = session
        self.repo = RawMessageRepository(session)
        self.parser_pipeline = parser_pipeline or ParserPipeline()
        self._dedup_service = dedup_service

    async def ingest(self, data: RawMessageCreate) -> RawMessage:
        """Idempotent ingest: returns existing or creates new RawMessage.

        - If (channel_id, tg_message_id) exists → return existing, NO modification.
        - If new → create with initial states:
            ingest_status = stored
            parse_status  = parse_pending
            dedup_status  = dedup_pending
            parse_attempts = 0
        """
        existing = await self.repo.get_by_channel_and_msg_id(
            data.channel_id, data.tg_message_id
        )
        if existing is not None:
            logger.info(
                "RawMessage channel_id=%s tg_message_id=%s already exists (id=%s), returning existing",
                data.channel_id,
                data.tg_message_id,
                existing.id,
            )
            return existing

        content_hash = _compute_content_hash(data.raw_text)

        raw_message = RawMessage(
            channel_id=data.channel_id,
            tg_message_id=data.tg_message_id,
            raw_text=data.raw_text,
            raw_media_refs=data.raw_media_refs,
            raw_payload=data.raw_payload,
            content_hash=content_hash,
            published_at=data.published_at,
            received_at=datetime.now(timezone.utc),
            ingest_status="stored",
            parse_status="parse_pending",
            dedup_status="dedup_pending",
            parse_attempts=0,
        )
        await self.repo.create(raw_message)
        await self.session.commit()
        logger.info(
            "Ingested RawMessage id=%s channel_id=%s tg_message_id=%s",
            raw_message.id,
            raw_message.channel_id,
            raw_message.tg_message_id,
        )
        return raw_message

    async def get_by_id(self, raw_msg_id: int) -> RawMessage | None:
        return await self.repo.get_by_id(raw_msg_id)

    async def get_by_channel_and_msg_id(
        self, channel_id: int, tg_message_id: int
    ) -> RawMessage | None:
        return await self.repo.get_by_channel_and_msg_id(channel_id, tg_message_id)

    async def parse_and_persist(self, raw_msg_id: int) -> RawMessage:
        """Run ParserPipeline and persist its outcome on one RawMessage.

        This is the P2-C boundary: it only updates parser-owned RawMessage fields.
        It does not create or update Work, Resource, dedup, or notification data.
        """
        raw_message = await self.repo.get_by_id(raw_msg_id)
        if raw_message is None:
            raise LookupError(f"RawMessage id={raw_msg_id} not found")

        raw_message.parse_attempts = (raw_message.parse_attempts or 0) + 1
        raw_message.parser_version = PARSER_VERSION
        raw_message.rule_version = RULE_VERSION

        try:
            resources = await self.parser_pipeline.parse(
                raw_message.raw_text,
                raw_message_id=raw_message.id,
            )
            if not resources:
                raw_message.parsed_data = None
                raw_message.parse_status = "parse_failed"
                raw_message.last_parse_error = EMPTY_PARSE_ERROR
            else:
                raw_message.parsed_data = [
                    resource.model_dump(mode="json") for resource in resources
                ]
                raw_message.parse_status = "parsed"
                raw_message.last_parse_error = None
        except Exception as exc:
            raw_message.parsed_data = None
            raw_message.parse_status = "parse_failed"
            raw_message.last_parse_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "ParserPipeline failed for RawMessage id=%s",
                raw_message.id,
            )

        raw_message.last_parsed_at = datetime.now(timezone.utc)
        await self.session.commit()
        await self.session.refresh(raw_message)
        return raw_message

    async def dedup_and_persist(
        self,
        raw_msg_id: int,
        event_bus: EventBus | None = None,
    ) -> RawMessage:
        """Run DedupService for each parsed resource in a RawMessage.

        Status machine:
          - parsed_data is None or []          → dedup_status = skipped
          - at least one DedupResult.is_new    → dedup_status = new
          - all DedupResult.is_new is False    → dedup_status = matched
          - any exception → rollback, reload, keep dedup_status = dedup_pending

        Transaction boundary: all dedup operations share one AsyncSession;
        commit is called here (the DedupService only flushes). Resource events
        are published only after that commit succeeds.
        """
        raw_message = await self.repo.get_by_id(raw_msg_id)
        if raw_message is None:
            raise LookupError(f"RawMessage id={raw_msg_id} not found")

        # No parsed data → skip
        if not raw_message.parsed_data:
            raw_message.dedup_status = "skipped"
            await self.session.commit()
            await self.session.refresh(raw_message)
            return raw_message

        dedup_service = self._dedup_service or DedupService(self.session)
        dedup_results: list[DedupResult] = []
        try:
            any_new = False
            for item in raw_message.parsed_data:
                parsed = deserialize_parsed_resource(item)
                normalized = normalize_resource(parsed)
                result = await dedup_service.dedup(
                    normalized=normalized,
                    parsed=parsed,
                    raw_message_id=raw_message.id,
                    channel_id=raw_message.channel_id,
                )
                dedup_results.append(result)
                if result.is_new:
                    any_new = True

            raw_message.dedup_status = "new" if any_new else "matched"
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            # ORM state is stale after rollback → reload
            raw_message = await self.repo.get_by_id(raw_msg_id)
            assert raw_message is not None  # confirmed to exist above
            raw_message.dedup_status = "dedup_pending"
            await self.session.commit()
            logger.exception(
                "DedupService failed for RawMessage id=%s", raw_msg_id,
            )
        else:
            if event_bus is not None:
                for result in dedup_results:
                    event = None
                    if result.is_new:
                        event = ResourceCreated(
                            resource_id=result.resource_id,
                            work_id=result.work_id,
                            raw_message_id=raw_msg_id,
                            source_count=result.source_count,
                        )
                    elif result.created_source or result.created_link_count > 0:
                        event = ResourceMerged(
                            resource_id=result.resource_id,
                            work_id=result.work_id,
                            raw_message_id=raw_msg_id,
                            source_count=result.source_count,
                            created_link_count=result.created_link_count,
                            created_source=result.created_source,
                        )

                    if event is not None:
                        try:
                            await event_bus.publish(event)
                        except Exception:
                            logger.exception(
                                "EventBus failed to publish %s for RawMessage id=%s",
                                type(event).__name__,
                                raw_msg_id,
                            )

        await self.session.refresh(raw_message)
        return raw_message

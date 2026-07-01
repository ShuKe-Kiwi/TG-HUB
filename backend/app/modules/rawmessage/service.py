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

from app.infra.logger import get_logger
from app.modules.parser.pipeline.core import (
    PARSER_VERSION,
    RULE_VERSION,
    ParserPipeline,
)
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.repository import RawMessageRepository
from app.modules.rawmessage.schema import RawMessageCreate

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
    ) -> None:
        self.session = session
        self.repo = RawMessageRepository(session)
        self.parser_pipeline = parser_pipeline or ParserPipeline()

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

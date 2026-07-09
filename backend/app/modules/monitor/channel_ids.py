"""Telegram channel id canonicalization helpers.

The project stores Telegram channel identity as Telethon's marked peer id,
which is the same form exposed by ``event.chat_id`` for channels.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

ChannelIdCanonicalizationStatus = Literal["valid", "invalid_source_ref"]
ChannelIdCanonicalizationErrorCode = Literal[
    "SOURCE_REF_NOT_NUMERIC",
    "SOURCE_REF_OUT_OF_RANGE",
    "SOURCE_REF_NOT_CANONICAL",
]

_NUMERIC_REF_RE = re.compile(r"-?\d+")
_TELETHON_MARKED_CHANNEL_ID_RE = re.compile(r"-100\d+")


class CanonicalChannelIdResult(BaseModel):
    """Stable, desensitized result for a Telegram channel id source ref."""

    model_config = ConfigDict(frozen=True)

    status: ChannelIdCanonicalizationStatus
    canonical_tg_id: int | None = None
    error_code: ChannelIdCanonicalizationErrorCode | None = None
    masked_source_ref: str
    channel_id_canonical_form: Literal["telethon_marked_peer_id"] = (
        "telethon_marked_peer_id"
    )


def canonicalize_source_channel_id(
    source_ref: str | int,
) -> CanonicalChannelIdResult:
    """Validate source_ref as the project's canonical Telegram channel id.

    Canonical form is Telethon's channel marked peer id, for example
    ``-1001234567890``. Bare Telegram entity ids are deliberately rejected
    instead of guessed or converted.
    """
    raw = str(source_ref).strip()
    masked = _mask_source_ref(raw)
    if not raw or not _NUMERIC_REF_RE.fullmatch(raw):
        return CanonicalChannelIdResult(
            status="invalid_source_ref",
            error_code="SOURCE_REF_NOT_NUMERIC",
            masked_source_ref=masked,
        )

    if raw == "-100":
        return CanonicalChannelIdResult(
            status="invalid_source_ref",
            error_code="SOURCE_REF_OUT_OF_RANGE",
            masked_source_ref=masked,
        )

    if not _TELETHON_MARKED_CHANNEL_ID_RE.fullmatch(raw):
        return CanonicalChannelIdResult(
            status="invalid_source_ref",
            error_code="SOURCE_REF_NOT_CANONICAL",
            masked_source_ref=masked,
        )

    return CanonicalChannelIdResult(
        status="valid",
        canonical_tg_id=int(raw),
        masked_source_ref=masked,
    )


def _mask_source_ref(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 2:
        return "*" * len(value)
    if len(value) <= 6:
        return f"{value[0]}***{value[-1]}"
    return f"{value[:2]}***{value[-2:]}"

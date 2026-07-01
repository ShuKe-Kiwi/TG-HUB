"""DedupService DTOs — per ARCHITECTURE.md V2.1-final §2.3-2.6."""

from pydantic import BaseModel, Field


class DedupResult(BaseModel):
    """Result of a single dedup/merge operation.

    Attributes:
        resource_id: The Resource.id that was created or merged into.
        work_id: The Work.id that the Resource belongs to.
        is_new: True if a new Resource was created (not merged).
        matched_reason: Human-readable explanation of the match decision.
        source_count: Total number of ResourceSource records for this Resource.
        created_link_count: How many new ResourceLinks were created this call.
        created_source: Whether a new ResourceSource was created this call.
    """

    resource_id: int
    work_id: int
    is_new: bool
    matched_reason: str
    source_count: int = Field(ge=0)
    created_link_count: int = Field(ge=0)
    created_source: bool

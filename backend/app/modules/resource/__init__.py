"""Resource Registry persistence models and repositories."""

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

__all__ = [
    "Resource",
    "ResourceLink",
    "ResourceLinkRepository",
    "ResourceRepository",
    "ResourceSource",
    "ResourceSourceRepository",
    "Work",
    "WorkRepository",
]

"""Pure Normalizer + Fingerprint functions for P3-A."""

from app.modules.normalizer.core import (
    NormalizedEpisode,
    NormalizedResource,
    normalize_episode,
    normalize_resource,
    normalize_title,
)
from app.modules.normalizer.fingerprint import (
    build_content_fingerprint,
    build_episode_key,
    build_resource_key,
    build_work_key,
)

__all__ = [
    "NormalizedEpisode",
    "NormalizedResource",
    "build_content_fingerprint",
    "build_episode_key",
    "build_resource_key",
    "build_work_key",
    "normalize_episode",
    "normalize_resource",
    "normalize_title",
]

"""Application boundary for safe watchlist reads and atomic replacement."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import Settings, settings
from app.modules.monitor.config import WatchlistConfig

WatchlistStatus = Literal["valid", "missing", "invalid", "unsafe"]
WatchlistErrorCode = Literal[
    "WATCHLIST_NOT_FOUND",
    "WATCHLIST_SCHEMA_INVALID",
    "WATCHLIST_REVISION_CONFLICT",
    "WATCHLIST_WRITE_FAILED",
    "WATCHLIST_PATH_UNSAFE",
    "WATCHLIST_PARENT_NOT_WRITABLE",
    "WATCHLIST_CURRENT_FILE_INVALID",
]


class WatchlistServiceError(RuntimeError):
    """Stable error raised by the watchlist application boundary."""

    def __init__(self, error_code: WatchlistErrorCode) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class WatchlistValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    config: WatchlistConfig | None
    errors: list[str]
    report_desensitized: Literal["yes"] = "yes"


class WatchlistSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: WatchlistStatus
    revision: str | None
    config: WatchlistConfig | None
    source_channel_count: int
    watch_title_count: int
    enabled_source_channel_count: int
    enabled_watch_title_count: int
    watchlist_schema_version: Literal[1] = 1
    error_code: WatchlistErrorCode | None = None
    report_desensitized: Literal["yes"] = "yes"


class WatchlistReplaceResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["replaced"] = "replaced"
    previous_revision: str | None
    revision: str
    source_channel_count: int
    watch_title_count: int
    recovered: bool
    watchlist_schema_version: Literal[1] = 1
    report_desensitized: Literal["yes"] = "yes"


class WatchlistApplicationService:
    """Own optimistic concurrency and file replacement for one watchlist."""

    def __init__(
        self,
        app_settings: Settings | None = None,
        *,
        path: str | Path | None = None,
        before_replace: Callable[[], None] | None = None,
    ) -> None:
        resolved_settings = app_settings or settings
        self.path = Path(path or resolved_settings.WATCHLIST_PATH).expanduser()
        self._write_lock = asyncio.Lock()
        self._before_replace = before_replace

    async def get_snapshot(self) -> WatchlistSnapshot:
        return await asyncio.to_thread(self._read_snapshot)

    def validate(
        self,
        candidate: WatchlistConfig | Mapping[str, Any],
    ) -> WatchlistValidationResult:
        errors: list[str] = []
        if isinstance(candidate, Mapping):
            errors.extend(_validate_raw_aliases(candidate))
        try:
            config = (
                candidate
                if isinstance(candidate, WatchlistConfig)
                else WatchlistConfig.model_validate(candidate)
            )
        except ValidationError:
            return WatchlistValidationResult(
                valid=False,
                config=None,
                errors=["schema_invalid"],
            )

        errors.extend(_validate_title_name_uniqueness(config))
        errors.extend(_validate_source_ref_uniqueness(config))
        return WatchlistValidationResult(
            valid=not errors,
            config=config if not errors else None,
            errors=list(dict.fromkeys(errors)),
        )

    async def replace(
        self,
        candidate: WatchlistConfig | Mapping[str, Any],
        expected_revision: str | None,
        *,
        recovery_confirmed: bool = False,
    ) -> WatchlistReplaceResult:
        validation = self.validate(candidate)
        if not validation.valid or validation.config is None:
            raise WatchlistServiceError("WATCHLIST_SCHEMA_INVALID")

        async with self._write_lock:
            return await asyncio.to_thread(
                self._replace_sync,
                validation.config,
                expected_revision,
                recovery_confirmed,
            )

    def _read_snapshot(self) -> WatchlistSnapshot:
        path_error = self._path_error()
        if path_error is not None:
            return _empty_snapshot("unsafe", path_error)
        if not self.path.exists():
            return _empty_snapshot("missing", "WATCHLIST_NOT_FOUND")

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping) or _validate_raw_aliases(raw):
                return _empty_snapshot(
                    "invalid", "WATCHLIST_SCHEMA_INVALID"
                )
            config = WatchlistConfig.model_validate(raw)
            if (
                _validate_title_name_uniqueness(config)
                or _validate_source_ref_uniqueness(config)
            ):
                return _invalid_snapshot_for_config(
                    config,
                    "WATCHLIST_SCHEMA_INVALID",
                )
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
            return _empty_snapshot("invalid", "WATCHLIST_SCHEMA_INVALID")

        return _snapshot_for_config(config)

    def _replace_sync(
        self,
        config: WatchlistConfig,
        expected_revision: str | None,
        recovery_confirmed: bool,
    ) -> WatchlistReplaceResult:
        current = self._read_snapshot()
        self._assert_replace_allowed(
            current,
            expected_revision=expected_revision,
            recovery_confirmed=recovery_confirmed,
        )
        self._assert_parent_writable()

        serialized = _serialized_file_bytes(config)
        target_mode = self._target_mode()
        temporary_path: Path | None = None
        try:
            descriptor, raw_temp_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary_path = Path(raw_temp_path)
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), target_mode)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())

            if self._before_replace is not None:
                self._before_replace()

            latest = self._read_snapshot()
            if latest.status == "unsafe":
                raise WatchlistServiceError("WATCHLIST_PATH_UNSAFE")
            if (
                latest.status != current.status
                or latest.revision != current.revision
            ):
                raise WatchlistServiceError(
                    "WATCHLIST_REVISION_CONFLICT"
                )

            os.replace(temporary_path, self.path)
            temporary_path = None
            _fsync_directory(self.path.parent)
        except WatchlistServiceError:
            raise
        except OSError as exc:
            raise WatchlistServiceError("WATCHLIST_WRITE_FAILED") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

        revision = _revision(config)
        return WatchlistReplaceResult(
            previous_revision=current.revision,
            revision=revision,
            source_channel_count=len(config.source_channels),
            watch_title_count=len(config.watch_titles),
            recovered=current.status in {"missing", "invalid"},
        )

    def _assert_replace_allowed(
        self,
        current: WatchlistSnapshot,
        *,
        expected_revision: str | None,
        recovery_confirmed: bool,
    ) -> None:
        if current.status == "unsafe":
            raise WatchlistServiceError("WATCHLIST_PATH_UNSAFE")
        if current.status == "invalid":
            if expected_revision is not None or not recovery_confirmed:
                raise WatchlistServiceError(
                    "WATCHLIST_CURRENT_FILE_INVALID"
                )
            return
        if current.status == "missing":
            if expected_revision is not None or not recovery_confirmed:
                raise WatchlistServiceError("WATCHLIST_NOT_FOUND")
            return
        if expected_revision != current.revision:
            raise WatchlistServiceError("WATCHLIST_REVISION_CONFLICT")

    def _path_error(self) -> WatchlistErrorCode | None:
        if self.path.is_symlink() or (self.path.exists() and self.path.is_dir()):
            return "WATCHLIST_PATH_UNSAFE"
        return None

    def _assert_parent_writable(self) -> None:
        parent = self.path.parent
        if (
            not parent.exists()
            or not parent.is_dir()
            or not os.access(parent, os.W_OK)
        ):
            raise WatchlistServiceError(
                "WATCHLIST_PARENT_NOT_WRITABLE"
            )

    def _target_mode(self) -> int:
        if not self.path.exists():
            return 0o600
        try:
            return stat.S_IMODE(self.path.stat(follow_symlinks=False).st_mode)
        except OSError as exc:
            raise WatchlistServiceError("WATCHLIST_WRITE_FAILED") from exc


def _empty_snapshot(
    status: WatchlistStatus,
    error_code: WatchlistErrorCode,
) -> WatchlistSnapshot:
    return WatchlistSnapshot(
        status=status,
        revision=None,
        config=None,
        source_channel_count=0,
        watch_title_count=0,
        enabled_source_channel_count=0,
        enabled_watch_title_count=0,
        error_code=error_code,
    )


def _snapshot_for_config(config: WatchlistConfig) -> WatchlistSnapshot:
    return WatchlistSnapshot(
        status="valid",
        revision=_revision(config),
        config=config,
        source_channel_count=len(config.source_channels),
        watch_title_count=len(config.watch_titles),
        enabled_source_channel_count=len(config.enabled_source_refs()),
        enabled_watch_title_count=len(config.enabled_watch_titles()),
    )


def _invalid_snapshot_for_config(
    config: WatchlistConfig,
    error_code: WatchlistErrorCode,
) -> WatchlistSnapshot:
    return WatchlistSnapshot(
        status="invalid",
        revision=None,
        config=config,
        source_channel_count=len(config.source_channels),
        watch_title_count=len(config.watch_titles),
        enabled_source_channel_count=len(config.enabled_source_refs()),
        enabled_watch_title_count=len(config.enabled_watch_titles()),
        error_code=error_code,
    )


def _canonical_bytes(config: WatchlistConfig) -> bytes:
    value = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return value.encode("utf-8")


def _serialized_file_bytes(config: WatchlistConfig) -> bytes:
    value = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return (value + "\n").encode("utf-8")


def _revision(config: WatchlistConfig) -> str:
    return hashlib.sha256(_canonical_bytes(config)).hexdigest()


def watchlist_revision(config: WatchlistConfig) -> str:
    """Return the canonical revision shared by runtime and backup snapshots."""
    return _revision(config)


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.strip()).casefold()
    return " ".join(normalized.split())


def _validate_raw_aliases(candidate: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    titles = candidate.get("watch_titles", [])
    if not isinstance(titles, list):
        return errors
    for title_index, item in enumerate(titles):
        if not isinstance(item, Mapping):
            continue
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list):
            continue
        for alias_index, alias in enumerate(aliases):
            if not isinstance(alias, str) or not alias.strip():
                errors.append(
                    f"empty_alias:{title_index}:{alias_index}"
                )
    return errors


def _validate_title_name_uniqueness(
    config: WatchlistConfig,
) -> list[str]:
    errors: list[str] = []
    owners: dict[str, str] = {}
    for index, item in enumerate(config.watch_titles):
        names = [
            ("title", item.title),
            *[("alias", value) for value in item.aliases],
        ]
        for kind, value in names:
            normalized = _normalized_name(value)
            owner = f"{index}:{kind}"
            previous = owners.get(normalized)
            if previous is not None:
                errors.append(f"duplicate_watch_name:{previous}:{owner}")
            else:
                owners[normalized] = owner
    return errors


def _validate_source_ref_uniqueness(
    config: WatchlistConfig,
) -> list[str]:
    errors: list[str] = []
    owners: dict[str, int] = {}
    for index, item in enumerate(config.source_channels):
        normalized = _normalized_source_ref(item.ref)
        previous = owners.get(normalized)
        if previous is not None:
            errors.append(f"duplicate_source_ref:{previous}:{index}")
        else:
            owners[normalized] = index
    return errors


def _normalized_source_ref(value: str) -> str:
    ref = unicodedata.normalize("NFKC", value.strip())
    if ref.startswith("@"):
        return f"username:{ref[1:].casefold()}"
    if ref.lstrip("-").isdigit():
        try:
            return f"numeric:{int(ref)}"
        except ValueError:
            pass

    parsed = urlparse(ref)
    if parsed.hostname and parsed.hostname.casefold() in {"t.me", "www.t.me"}:
        parts = [part for part in parsed.path.split("/") if part]
        if parts:
            return f"username:{parts[0].casefold()}"
    return f"raw:{ref.casefold()}"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from app.modules.monitor.watchlist_service import (
    WatchlistApplicationService,
    WatchlistServiceError,
)


def _candidate(title: str = "家业") -> dict[str, object]:
    return {
        "source_channels": [
            {"ref": "https://t.me/example", "enabled": True}
        ],
        "watch_titles": [
            {"title": title, "enabled": True, "aliases": ["家 业"]}
        ],
    }


def _write(path: Path, candidate: dict[str, object]) -> None:
    path.write_text(
        json.dumps(candidate, ensure_ascii=False),
        encoding="utf-8",
    )


async def test_snapshot_returns_canonical_revision_and_counts(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    _write(path, _candidate())
    service = WatchlistApplicationService(path=path)

    first = await service.get_snapshot()
    path.write_text(
        json.dumps(_candidate(), ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    second = await service.get_snapshot()

    assert first.status == "valid"
    assert first.revision == second.revision
    assert first.source_channel_count == 1
    assert first.enabled_watch_title_count == 1
    assert first.watchlist_schema_version == 1


@pytest.mark.parametrize(
    ("content", "status", "error_code"),
    [
        (None, "missing", "WATCHLIST_NOT_FOUND"),
        ("not json", "invalid", "WATCHLIST_SCHEMA_INVALID"),
    ],
)
async def test_snapshot_reports_missing_and_invalid_without_raw_content(
    tmp_path: Path,
    content: str | None,
    status: str,
    error_code: str,
) -> None:
    path = tmp_path / "watchlist.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    snapshot = await WatchlistApplicationService(path=path).get_snapshot()

    assert snapshot.status == status
    assert snapshot.revision is None
    assert snapshot.config is None
    assert snapshot.error_code == error_code
    assert "not json" not in snapshot.model_dump_json()


async def test_snapshot_treats_empty_alias_as_invalid(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    candidate = _candidate()
    candidate["watch_titles"][0]["aliases"] = [" "]
    _write(path, candidate)

    snapshot = await WatchlistApplicationService(path=path).get_snapshot()

    assert snapshot.status == "invalid"
    assert snapshot.revision is None


def test_validation_rejects_empty_alias_before_model_normalization(tmp_path: Path) -> None:
    candidate = _candidate()
    candidate["watch_titles"][0]["aliases"] = ["   "]

    result = WatchlistApplicationService(
        path=tmp_path / "watchlist.json"
    ).validate(candidate)

    assert result.valid is False
    assert result.config is None
    assert result.errors == ["empty_alias:0:0"]


def test_validation_uses_global_nfkc_casefold_name_uniqueness(tmp_path: Path) -> None:
    candidate = {
        "source_channels": [],
        "watch_titles": [
            {"title": "Gundam Ｘ", "enabled": False, "aliases": []},
            {"title": "another", "enabled": True, "aliases": ["gundam x"]},
        ],
    }

    result = WatchlistApplicationService(
        path=tmp_path / "watchlist.json"
    ).validate(candidate)

    assert result.valid is False
    assert result.errors[0].startswith("duplicate_watch_name:")


async def test_replace_preserves_existing_mode_and_updates_revision(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    _write(path, _candidate())
    path.chmod(0o640)
    service = WatchlistApplicationService(path=path)
    before = await service.get_snapshot()

    result = await service.replace(_candidate("百花杀"), before.revision)
    after = await service.get_snapshot()

    assert result.previous_revision == before.revision
    assert result.revision == after.revision
    assert result.recovered is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert after.config.watch_titles[0].title == "百花杀"


async def test_missing_file_requires_explicit_recovery_and_uses_0600(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    service = WatchlistApplicationService(path=path)

    with pytest.raises(WatchlistServiceError) as exc_info:
        await service.replace(_candidate(), None)
    assert exc_info.value.error_code == "WATCHLIST_NOT_FOUND"

    result = await service.replace(
        _candidate(),
        None,
        recovery_confirmed=True,
    )

    assert result.recovered is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_invalid_file_requires_explicit_recovery(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text("broken", encoding="utf-8")
    service = WatchlistApplicationService(path=path)

    with pytest.raises(WatchlistServiceError) as exc_info:
        await service.replace(_candidate(), None)
    assert exc_info.value.error_code == "WATCHLIST_CURRENT_FILE_INVALID"

    result = await service.replace(
        _candidate(),
        None,
        recovery_confirmed=True,
    )
    assert result.recovered is True
    assert (await service.get_snapshot()).status == "valid"


async def test_stale_revision_does_not_change_file(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    _write(path, _candidate())
    original = path.read_bytes()

    with pytest.raises(WatchlistServiceError) as exc_info:
        await WatchlistApplicationService(path=path).replace(
            _candidate("百花杀"),
            "stale",
        )

    assert exc_info.value.error_code == "WATCHLIST_REVISION_CONFLICT"
    assert path.read_bytes() == original


async def test_two_concurrent_replaces_have_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    _write(path, _candidate())
    service = WatchlistApplicationService(path=path)
    revision = (await service.get_snapshot()).revision

    outcomes = await asyncio.gather(
        service.replace(_candidate("百花杀"), revision),
        service.replace(_candidate("主角"), revision),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    error = next(item for item in outcomes if isinstance(item, Exception))
    assert isinstance(error, WatchlistServiceError)
    assert error.error_code == "WATCHLIST_REVISION_CONFLICT"


async def test_external_change_before_replace_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    _write(path, _candidate())
    initial = await WatchlistApplicationService(path=path).get_snapshot()

    def external_write() -> None:
        _write(path, _candidate("外部修改"))

    service = WatchlistApplicationService(
        path=path,
        before_replace=external_write,
    )
    with pytest.raises(WatchlistServiceError) as exc_info:
        await service.replace(_candidate("管理台修改"), initial.revision)

    assert exc_info.value.error_code == "WATCHLIST_REVISION_CONFLICT"
    assert "外部修改" in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.tmp"))


async def test_symlink_target_is_rejected_without_touching_real_file(tmp_path: Path) -> None:
    real_path = tmp_path / "real.json"
    _write(real_path, _candidate())
    link_path = tmp_path / "watchlist.json"
    link_path.symlink_to(real_path)
    original = real_path.read_bytes()
    service = WatchlistApplicationService(path=link_path)

    snapshot = await service.get_snapshot()
    with pytest.raises(WatchlistServiceError) as exc_info:
        await service.replace(
            _candidate("百花杀"),
            None,
            recovery_confirmed=True,
        )

    assert snapshot.status == "unsafe"
    assert exc_info.value.error_code == "WATCHLIST_PATH_UNSAFE"
    assert real_path.read_bytes() == original


async def test_missing_parent_is_reported_without_creation(tmp_path: Path) -> None:
    parent = tmp_path / "missing"
    service = WatchlistApplicationService(path=parent / "watchlist.json")

    with pytest.raises(WatchlistServiceError) as exc_info:
        await service.replace(
            _candidate(),
            None,
            recovery_confirmed=True,
        )

    assert exc_info.value.error_code == "WATCHLIST_PARENT_NOT_WRITABLE"
    assert parent.exists() is False


async def test_write_failure_preserves_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "watchlist.json"
    _write(path, _candidate())
    original = path.read_bytes()
    service = WatchlistApplicationService(path=path)
    revision = (await service.get_snapshot()).revision

    def fail_replace(source, target):
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(WatchlistServiceError) as exc_info:
        await service.replace(_candidate("百花杀"), revision)

    assert exc_info.value.error_code == "WATCHLIST_WRITE_FAILED"
    assert path.read_bytes() == original

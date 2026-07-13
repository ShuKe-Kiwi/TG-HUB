import asyncio
import fcntl
import json
import os
import stat
import threading
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.modules.monitor.heartbeat import (
    CompositeHeartbeatSink,
    JsonlHeartbeatSink,
)


class FakeHeartbeat(BaseModel):
    monitor_state: str = "listening"
    events_seen_total: int = 2
    detail: str = ""


async def test_jsonl_heartbeat_sink_writes_flushes_and_closes(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "heartbeat.jsonl"
    sink = JsonlHeartbeatSink(path)

    await sink.emit(FakeHeartbeat())
    await sink.emit(FakeHeartbeat(events_seen_total=3))
    await sink.aclose()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["events_seen_total"] for line in lines] == [2, 3]
    assert sink.closed is True
    assert sink.error_count == 0


async def test_jsonl_heartbeat_write_failure_is_isolated(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    sink = JsonlHeartbeatSink(parent_file / "heartbeat.jsonl")

    await sink.emit(FakeHeartbeat())
    await sink.aclose()

    assert sink.error_count == 1
    assert sink.last_error_code == "HEARTBEAT_PATH_INVALID"


async def test_jsonl_heartbeat_is_short_open_and_private(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "heartbeat.jsonl"
    sink = JsonlHeartbeatSink(path)

    await sink.emit(FakeHeartbeat())

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((path.parent / "heartbeat.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not hasattr(sink, "_handle")


async def test_jsonl_heartbeat_rejects_symlink_target(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    target = runtime / "target.jsonl"
    target.write_text("", encoding="utf-8")
    path = runtime / "heartbeat.jsonl"
    path.symlink_to(target)
    sink = JsonlHeartbeatSink(path)

    await sink.emit(FakeHeartbeat())

    assert sink.status().status == "path_invalid"
    assert sink.status().error_code == "HEARTBEAT_PATH_INVALID"


async def test_jsonl_heartbeat_rejects_symlink_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "runtime"
    linked.symlink_to(real, target_is_directory=True)
    sink = JsonlHeartbeatSink(linked / "heartbeat.jsonl")

    await sink.emit(FakeHeartbeat())

    assert sink.status().status == "path_invalid"
    assert not (real / "heartbeat.jsonl").exists()


async def test_jsonl_heartbeat_rejects_oversized_payload(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "heartbeat.jsonl"
    sink = JsonlHeartbeatSink(path)

    await sink.emit(FakeHeartbeat(detail="x" * (64 * 1024)))

    assert sink.status().status == "write_failed"
    assert sink.status().error_code == "HEARTBEAT_PAYLOAD_TOO_LARGE"
    assert not path.exists()


async def test_cancelled_emit_waits_for_worker_before_close(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "runtime" / "heartbeat.jsonl"
    sink = JsonlHeartbeatSink(path)
    started = threading.Event()
    release = threading.Event()
    original = sink._write_blocking

    def blocked_write(payload: bytes):
        started.set()
        assert release.wait(timeout=2)
        return original(payload)

    monkeypatch.setattr(sink, "_write_blocking", blocked_write)
    emit_task = asyncio.create_task(sink.emit(FakeHeartbeat()))
    assert await asyncio.to_thread(started.wait, 1)

    emit_task.cancel()
    close_task = asyncio.create_task(sink.aclose())
    await asyncio.sleep(0)
    assert close_task.done() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await emit_task
    await close_task

    assert sink.closed is True
    assert sink.status().status == "closed"
    assert path.read_text(encoding="utf-8").count("\n") == 1


async def test_composite_isolates_failure_and_closes_all() -> None:
    calls: list[str] = []

    class FailedSink:
        last_error_code = "TEST_FAILED"

        async def emit(self, heartbeat) -> None:
            calls.append("failed_emit")
            raise RuntimeError("hidden")

        async def aclose(self) -> None:
            calls.append("failed_close")
            raise RuntimeError("hidden")

    class HealthySink:
        async def emit(self, heartbeat) -> None:
            calls.append("healthy_emit")

        async def aclose(self) -> None:
            calls.append("healthy_close")

    sink = CompositeHeartbeatSink(FailedSink(), HealthySink())
    await sink.emit(FakeHeartbeat())
    await sink.aclose()
    await sink.aclose()

    assert calls == [
        "failed_emit",
        "healthy_emit",
        "failed_close",
        "healthy_close",
    ]
    assert sink.error_count == 2


async def test_emit_after_close_does_not_reopen_file(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "heartbeat.jsonl"
    sink = JsonlHeartbeatSink(path)
    await sink.aclose()

    await sink.emit(FakeHeartbeat())

    assert sink.status().status == "closed"
    assert sink.status().error_code == "HEARTBEAT_SINK_CLOSED"
    assert not path.exists()


async def test_jsonl_heartbeat_reports_flock_timeout(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    lock_path = runtime / "heartbeat.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    sink = JsonlHeartbeatSink(
        runtime / "heartbeat.jsonl",
        flock_timeout_seconds=0.01,
    )
    try:
        await sink.emit(FakeHeartbeat())
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert sink.status().status == "lock_timeout"
    assert sink.status().error_code == "HEARTBEAT_LOCK_TIMEOUT"


async def test_jsonl_heartbeat_reports_final_short_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sink = JsonlHeartbeatSink(tmp_path / "runtime" / "heartbeat.jsonl")
    monkeypatch.setattr(os, "write", lambda fd, payload: 0)

    await sink.emit(FakeHeartbeat())

    assert sink.status().status == "short_write"
    assert sink.status().error_code == "HEARTBEAT_SHORT_WRITE"

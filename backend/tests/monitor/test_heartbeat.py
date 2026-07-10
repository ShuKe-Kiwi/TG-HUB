import json
from pathlib import Path

from pydantic import BaseModel

from app.modules.monitor.heartbeat import JsonlHeartbeatSink


class FakeHeartbeat(BaseModel):
    monitor_state: str = "listening"
    events_seen_total: int = 2


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
    assert sink.last_error_code == "HEARTBEAT_WRITE_FAILED"

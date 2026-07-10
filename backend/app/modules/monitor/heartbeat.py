"""CLI-owned monitor heartbeat sinks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Protocol

from app.modules.monitor.runtime import MonitorHeartbeat


class HeartbeatSink(Protocol):
    async def emit(self, heartbeat: MonitorHeartbeat) -> None: ...

    async def aclose(self) -> None: ...


class NullHeartbeatSink:
    error_count = 0
    last_error_code: str | None = None

    async def emit(self, heartbeat: MonitorHeartbeat) -> None:
        return None

    async def aclose(self) -> None:
        return None


class JsonlHeartbeatSink:
    """Append desensitized heartbeats and own the output file lifecycle."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._handle = None
        self.error_count = 0
        self.last_error_code: str | None = None
        self.closed = False

    async def emit(self, heartbeat: MonitorHeartbeat) -> None:
        try:
            if self._handle is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self.path.open("a", encoding="utf-8")
            payload = json.dumps(
                heartbeat.model_dump(mode="json"),
                ensure_ascii=True,
                separators=(",", ":"),
            )
            self._handle.write(payload + "\n")
            self._handle.flush()
        except (OSError, TypeError, ValueError):
            self.error_count += 1
            self.last_error_code = "HEARTBEAT_WRITE_FAILED"

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        handle = self._handle
        self._handle = None
        if handle is not None:
            await asyncio.to_thread(handle.close)

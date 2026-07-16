"""Pure, fake-only orchestration helpers for 4D-4A contract tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from app.deploy.production_recovery_models import (
    ProductionCleanupRecord,
    ProductionRecoveryRecord,
    RecoveryPhase,
)
from app.deploy.production_recovery_record import TempRecoveryRecordStore


@dataclass
class FakeRecoveryAction:
    calls: int = 0
    result: object = None

    def __call__(self) -> object:
        self.calls += 1
        return self.result


class GuardedDatabaseCleanupPrimitive(Protocol):
    async def drop_once(
        self, record: ProductionCleanupRecord
    ) -> Literal["dropped", "absent"]: ...


class TempRecoveryOrchestrator:
    """Persists an intent before invoking one injected, non-production action."""

    def __init__(self, store: TempRecoveryRecordStore) -> None:
        self.store = store

    async def execute_after_intent(
        self,
        record: ProductionRecoveryRecord,
        intent_phase: RecoveryPhase,
        action: Callable[[], object],
    ) -> tuple[ProductionRecoveryRecord, object]:
        with self.store.operation_lease(record) as lease:
            durable = lease.advance(intent_phase)
            worker = asyncio.create_task(self._invoke(action))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    await worker
                finally:
                    raise
            return durable, result

    @staticmethod
    async def _invoke(action: Callable[[], object]) -> object:
        result = await asyncio.to_thread(action)
        if inspect.isawaitable(result):
            return await result
        return result

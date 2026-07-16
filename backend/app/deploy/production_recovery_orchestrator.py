"""Pure, fake-only orchestration helpers for 4D-4A contract tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.deploy.production_recovery_models import ProductionRecoveryRecord, RecoveryPhase
from app.deploy.production_recovery_record import TempRecoveryRecordStore


@dataclass
class FakeRecoveryAction:
    calls: int = 0
    result: object = None

    def __call__(self) -> object:
        self.calls += 1
        return self.result


class TempRecoveryOrchestrator:
    """Persists an intent before invoking one injected, non-production action."""

    def __init__(self, store: TempRecoveryRecordStore) -> None:
        self.store = store

    def execute_after_intent(
        self,
        record: ProductionRecoveryRecord,
        intent_phase: RecoveryPhase,
        action: Callable[[], object],
    ) -> tuple[ProductionRecoveryRecord, object]:
        durable = self.store.advance(record, intent_phase)
        return durable, action()

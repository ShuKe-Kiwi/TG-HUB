"""Single-use retention mutation authorization contracts for P6-Deploy-4D-3A."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import secrets
import stat
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Literal

from pydantic import Field, ValidationError, model_validator

from app.config import Settings
from app.deploy.backup_fs import (
    BackupFsError,
    atomic_write_bytes,
    backup_root_lock,
    create_bytes_if_absent,
    ensure_private_directory,
    fsync_directory,
    open_regular,
    read_regular_exact,
    validate_backup_root,
)
from app.deploy.backup_models import (
    BACKUP_ID_PATTERN,
    OPAQUE_ID_PATTERN,
    ContractModel,
)
from app.deploy.backup_retention import (
    TempMutationCapability,
    _canonical_json,
    _require_capability,
)

UTC = timezone.utc
MAX_AUTHORIZATION_BYTES = 64 * 1024
AUTHORIZATION_SCHEMA_VERSION = 1
IDENTITY_PATTERN = r"^(missing|[0-9a-f]{64})$"


class AuthorizationError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class BackupRootIdentity(ContractModel):
    configured_logical_root_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolved_device: int = Field(ge=0)
    resolved_inode: int = Field(ge=1)


class StableLockIdentity(ContractModel):
    resolved_device: int = Field(ge=0)
    resolved_inode: int = Field(ge=1)


class PinPayload(ContractModel):
    kind: Literal["pin"] = "pin"
    backup_id: str
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    expected_pin_identity: str = Field(pattern=IDENTITY_PATTERN)

    @model_validator(mode="after")
    def validate_backup_id(self) -> "PinPayload":
        _validate_backup_ids((self.backup_id,))
        return self


class UnpinPayload(ContractModel):
    kind: Literal["unpin"] = "unpin"
    backup_id: str
    expected_pin_identity: str = Field(pattern=IDENTITY_PATTERN)
    expected_reason_code: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$"
    )

    @model_validator(mode="after")
    def validate_backup_id(self) -> "UnpinPayload":
        _validate_backup_ids((self.backup_id,))
        return self


class PinReconcilePayload(ContractModel):
    kind: Literal["pin_reconcile"] = "pin_reconcile"
    predecessor_authorization_id: str
    predecessor_consumed_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_operation: Literal["pin", "unpin"]
    pin_operation_id: str
    pin_journal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pin_journal_phase: Literal[
        "planned", "mutation_started", "mutation_committed"
    ]
    backup_id: str
    intended_reason_code: str | None = Field(
        default=None, max_length=64, pattern=r"^[a-z0-9_]+$"
    )
    observed_before_pin_identity: str = Field(pattern=IDENTITY_PATTERN)
    intended_result_pin_identity: str = Field(pattern=IDENTITY_PATTERN)
    expected_current_pin_identity: str = Field(pattern=IDENTITY_PATTERN)
    reconcile_scope: Literal["settle_current_only"]
    acknowledge_current_partial_state: Literal[True]

    @model_validator(mode="after")
    def validate_ids(self) -> "PinReconcilePayload":
        _validate_opaque_id(self.predecessor_authorization_id)
        _validate_opaque_id(self.pin_operation_id)
        _validate_backup_ids((self.backup_id,))
        if self.original_operation == "pin" and not self.intended_reason_code:
            raise ValueError("pin reconcile requires intended reason")
        if self.original_operation == "unpin" and self.intended_reason_code is not None:
            raise ValueError("unpin reconcile forbids intended reason")
        return self


class RetentionApplyPayload(ContractModel):
    kind: Literal["retention_apply"] = "retention_apply"
    plan_id: str
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordered_candidate_backup_ids: tuple[str, ...]
    expected_candidate_count: int = Field(ge=1)
    expected_reclaim_bytes: int = Field(ge=0)
    acknowledge_non_transactional_multi_package_delete: Literal[True]
    acknowledge_terminal_metadata_retained: Literal[True]

    @model_validator(mode="after")
    def validate_candidates(self) -> "RetentionApplyPayload":
        _validate_opaque_id(self.plan_id)
        _validate_backup_ids(self.ordered_candidate_backup_ids)
        if self.expected_candidate_count != len(self.ordered_candidate_backup_ids):
            raise ValueError("candidate count mismatch")
        return self


class RetentionResumePayload(ContractModel):
    kind: Literal["retention_resume"] = "retention_resume"
    predecessor_authorization_id: str
    predecessor_consumed_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    journal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    journal_generation: int = Field(ge=1)
    already_deleted_backup_ids: tuple[str, ...]
    earliest_unfinished_backup_id: str
    earliest_unfinished_phase: Literal[
        "planned", "rename_started", "pending", "delete_started", "cleanup_required"
    ]
    remaining_ordered_candidate_ids: tuple[str, ...]
    resume_scope: Literal[
        "settle_current_only", "settle_and_continue_remaining"
    ]
    already_reclaimed_bytes: int = Field(ge=0)
    remaining_expected_reclaim_bytes: int = Field(ge=0)
    acknowledge_current_partial_state: Literal[True]

    @model_validator(mode="after")
    def validate_resume_order(self) -> "RetentionResumePayload":
        _validate_opaque_id(self.predecessor_authorization_id)
        _validate_opaque_id(self.plan_id)
        _validate_backup_ids(self.already_deleted_backup_ids)
        _validate_backup_ids(self.remaining_ordered_candidate_ids)
        if not BACKUP_ID_PATTERN.fullmatch(self.earliest_unfinished_backup_id):
            raise ValueError("invalid earliest unfinished backup id")
        if (
            not self.remaining_ordered_candidate_ids
            or self.remaining_ordered_candidate_ids[0]
            != self.earliest_unfinished_backup_id
        ):
            raise ValueError("earliest unfinished candidate must be first")
        if set(self.already_deleted_backup_ids) & set(
            self.remaining_ordered_candidate_ids
        ):
            raise ValueError("deleted and remaining candidates overlap")
        return self


AuthorizationPayload = (
    PinPayload
    | UnpinPayload
    | PinReconcilePayload
    | RetentionApplyPayload
    | RetentionResumePayload
)


class ProductionAuthorization(ContractModel):
    schema_version: Literal[1] = AUTHORIZATION_SCHEMA_VERSION
    authorization_id: str
    authorization_nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal[
        "pin", "unpin", "pin_reconcile", "retention_apply", "retention_resume"
    ]
    backup_root_identity: BackupRootIdentity
    stable_lock_identity: StableLockIdentity
    issued_at_utc: datetime
    expires_at_utc: datetime
    single_use: Literal[True]
    phase: Literal["issued", "consumed", "cancelled"] = "issued"
    consumed_at_utc: datetime | None = None
    payload: AuthorizationPayload = Field(discriminator="kind")

    @model_validator(mode="after")
    def validate_contract(self) -> "ProductionAuthorization":
        if not OPAQUE_ID_PATTERN.fullmatch(self.authorization_id):
            raise ValueError("invalid authorization id")
        if self.operation != self.payload.kind:
            raise ValueError("operation and payload mismatch")
        for value in (
            self.issued_at_utc,
            self.expires_at_utc,
            self.consumed_at_utc,
        ):
            if value is not None and (
                value.tzinfo is None
                or value.utcoffset() is None
                or value.utcoffset().total_seconds() != 0
            ):
                raise ValueError("authorization timestamps must be UTC")
        if self.expires_at_utc <= self.issued_at_utc:
            raise ValueError("authorization expiry must follow issuance")
        if self.phase == "consumed" and self.consumed_at_utc is None:
            raise ValueError("consumed authorization requires consumed_at")
        if self.phase != "consumed" and self.consumed_at_utc is not None:
            raise ValueError("only consumed authorization has consumed_at")
        return self


class AuthorizationResult(ContractModel):
    status: Literal["pass", "fail"]
    authorization_id: str | None
    phase: Literal["issued", "consumed", "cancelled", "unknown"]
    mutation_status: Literal["not_started", "completed", "failed", "unknown"]
    error_code: str | None = None
    report_desensitized: Literal["yes"] = "yes"


class TempAuthorizationIssuer:
    """Issuer available only for a capability-bound temporary backup root."""

    def __init__(
        self,
        settings: Settings,
        capability: TempMutationCapability,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = AuthorizationStore(settings, capability)
        self.clock = clock or (lambda: datetime.now(UTC))

    def issue(
        self,
        payload: AuthorizationPayload,
        *,
        expires_at_utc: datetime,
    ) -> tuple[ProductionAuthorization, str]:
        now = _as_utc(self.clock())
        authorization_id = secrets.token_hex(16)
        lock_identity = self.store.initialize_stable_lock(authorization_id)
        authorization = ProductionAuthorization(
            authorization_id=authorization_id,
            authorization_nonce=secrets.token_hex(32),
            operation=payload.kind,
            backup_root_identity=self.store.root_identity(),
            stable_lock_identity=lock_identity,
            issued_at_utc=now,
            expires_at_utc=_as_utc(expires_at_utc),
            single_use=True,
            payload=payload,
        )
        self.store.create(authorization)
        return authorization, authorization.authorization_nonce


class AuthorizationStore:
    def __init__(
        self, settings: Settings, capability: TempMutationCapability
    ) -> None:
        self.settings = settings
        self.capability = capability
        self.backup_root = settings.BACKUP_DIR.expanduser()
        self.root = self.backup_root.parent / "runtime" / "retention-authorizations"

    def root_identity(self) -> BackupRootIdentity:
        self._require_temp_root()
        info = self.backup_root.lstat()
        logical = hashlib.sha256(
            str(self.backup_root.resolve()).encode("utf-8")
        ).hexdigest()
        return BackupRootIdentity(
            configured_logical_root_id=logical,
            resolved_device=info.st_dev,
            resolved_inode=info.st_ino,
        )

    def create(self, authorization: ProductionAuthorization) -> None:
        self._require_temp_root()
        if (
            authorization.phase != "issued"
            or authorization.backup_root_identity != self.root_identity()
        ):
            raise AuthorizationError("BACKUP_AUTHORIZATION_INVALID")
        ensure_private_directory(self.root)
        lock_path, record_path = self._paths(authorization.authorization_id)
        if _lock_identity(lock_path) != authorization.stable_lock_identity:
            raise AuthorizationError("BACKUP_AUTHORIZATION_LOCK_INVALID")
        payload = _canonical_json(authorization.model_dump(mode="json"))
        if not create_bytes_if_absent(
            record_path, payload, token=authorization.authorization_id
        ):
            raise AuthorizationError("BACKUP_AUTHORIZATION_ALREADY_EXISTS")

    def read(self, authorization_id: str) -> ProductionAuthorization:
        self._require_temp_root()
        with self.lock_many((authorization_id,)) as lock_identities:
            value = self._read_locked(authorization_id)
            self._validate_lock_identity(value, lock_identities)
            return value

    def cancel(self, authorization_id: str, nonce: str) -> AuthorizationResult:
        self._require_temp_root()
        try:
            with self.lock_many((authorization_id,)) as lock_identities:
                value = self._read_locked(authorization_id)
                self._validate_lock_identity(value, lock_identities)
                self._validate_nonce(value, nonce)
                if value.phase != "issued":
                    raise AuthorizationError("BACKUP_AUTHORIZATION_ALREADY_CONSUMED")
                updated = value.model_copy(update={"phase": "cancelled"})
                self._write_locked(updated)
            return AuthorizationResult(
                status="pass", authorization_id=authorization_id,
                phase="cancelled", mutation_status="not_started",
            )
        except (AuthorizationError, BackupFsError, OSError, ValidationError) as exc:
            return _failure(authorization_id, exc)

    @contextmanager
    def consume(
        self,
        authorization_id: str,
        nonce: str,
        *,
        prepare_journal: Callable[[ProductionAuthorization], None],
        clock: Callable[[], datetime] | None = None,
    ) -> Iterator[ProductionAuthorization]:
        self._require_temp_root()
        clock_fn = clock or (lambda: datetime.now(UTC))
        first = self.read(authorization_id)
        predecessor_ids = _predecessor_ids(first.payload)
        lock_ids = tuple(sorted({authorization_id, *predecessor_ids}))
        with self.lock_many(lock_ids) as lock_identities:
            current = self._read_locked(authorization_id)
            self._validate_lock_identity(current, lock_identities)
            if _predecessor_ids(current.payload) != predecessor_ids:
                raise AuthorizationError("BACKUP_AUTHORIZATION_PREDECESSOR_INVALID")
            self._validate_consumable(current, nonce, _as_utc(clock_fn()))
            self._validate_predecessors(current, lock_identities)
            with backup_root_lock(self.backup_root, mode="exclusive"):
                current = self._read_locked(authorization_id)
                self._validate_lock_identity(current, lock_identities)
                if _predecessor_ids(current.payload) != predecessor_ids:
                    raise AuthorizationError(
                        "BACKUP_AUTHORIZATION_PREDECESSOR_INVALID"
                    )
                consumed_at = _as_utc(clock_fn())
                self._validate_consumable(current, nonce, consumed_at)
                self._validate_predecessors(current, lock_identities)
                prepare_journal(current)
                consumed = current.model_copy(
                    update={"phase": "consumed", "consumed_at_utc": consumed_at}
                )
                self._write_locked(consumed)
                yield consumed

    @contextmanager
    def lock_many(
        self, authorization_ids: tuple[str, ...]
    ) -> Iterator[dict[str, StableLockIdentity]]:
        ordered = tuple(sorted(set(authorization_ids)))
        with ExitStack() as stack:
            identities: dict[str, StableLockIdentity] = {}
            for authorization_id in ordered:
                lock_path, _ = self._paths(authorization_id)
                identities[authorization_id] = stack.enter_context(
                    _stable_lock(lock_path)
                )
            yield identities

    def _validate_predecessors(
        self,
        current: ProductionAuthorization,
        lock_identities: dict[str, StableLockIdentity],
    ) -> None:
        payload = current.payload
        if not isinstance(payload, (PinReconcilePayload, RetentionResumePayload)):
            return
        predecessor = self._read_locked(payload.predecessor_authorization_id)
        self._validate_lock_identity(predecessor, lock_identities)
        if predecessor.phase != "consumed":
            raise AuthorizationError("BACKUP_AUTHORIZATION_PREDECESSOR_INVALID")
        if _authorization_identity(predecessor) != payload.predecessor_consumed_identity:
            raise AuthorizationError("BACKUP_AUTHORIZATION_PREDECESSOR_INVALID")
        allowed = (
            {"pin", "unpin"}
            if isinstance(payload, PinReconcilePayload)
            else {"retention_apply", "retention_resume"}
        )
        if predecessor.operation not in allowed:
            raise AuthorizationError("BACKUP_AUTHORIZATION_PREDECESSOR_INVALID")
        if isinstance(payload, PinReconcilePayload):
            self._validate_pin_lineage(payload, predecessor)
        else:
            self._validate_retention_lineage(payload, predecessor)

    def _validate_pin_lineage(
        self,
        payload: PinReconcilePayload,
        predecessor: ProductionAuthorization,
    ) -> None:
        predecessor_payload = predecessor.payload
        if predecessor.operation != payload.original_operation:
            raise AuthorizationError("BACKUP_AUTHORIZATION_PREDECESSOR_INVALID")
        if isinstance(predecessor_payload, PinPayload):
            matches = (
                payload.original_operation == "pin"
                and predecessor_payload.backup_id == payload.backup_id
                and predecessor_payload.reason_code == payload.intended_reason_code
                and predecessor_payload.expected_pin_identity
                == payload.observed_before_pin_identity
            )
        elif isinstance(predecessor_payload, UnpinPayload):
            matches = (
                payload.original_operation == "unpin"
                and predecessor_payload.backup_id == payload.backup_id
                and payload.intended_reason_code is None
                and predecessor_payload.expected_pin_identity
                == payload.observed_before_pin_identity
            )
        else:
            matches = False
        if not matches:
            raise AuthorizationError("BACKUP_AUTHORIZATION_PREDECESSOR_INVALID")

    def _validate_retention_lineage(
        self,
        payload: RetentionResumePayload,
        predecessor: ProductionAuthorization,
    ) -> None:
        predecessor_payload = predecessor.payload
        if not isinstance(
            predecessor_payload, (RetentionApplyPayload, RetentionResumePayload)
        ) or (
            predecessor_payload.plan_id != payload.plan_id
            or predecessor_payload.plan_digest != payload.plan_digest
        ):
            raise AuthorizationError("BACKUP_AUTHORIZATION_PREDECESSOR_INVALID")

    def _read_locked(self, authorization_id: str) -> ProductionAuthorization:
        _, path = self._paths(authorization_id)
        try:
            return ProductionAuthorization.model_validate_json(
                read_regular_exact(path, max_bytes=MAX_AUTHORIZATION_BYTES)
            )
        except (BackupFsError, OSError, ValidationError) as exc:
            raise AuthorizationError("BACKUP_AUTHORIZATION_INVALID") from exc

    def _write_locked(self, authorization: ProductionAuthorization) -> None:
        _, path = self._paths(authorization.authorization_id)
        try:
            atomic_write_bytes(
                path,
                _canonical_json(authorization.model_dump(mode="json")),
                token=secrets.token_hex(8),
            )
        except (BackupFsError, OSError) as exc:
            raise AuthorizationError("BACKUP_AUTHORIZATION_WRITE_FAILED") from exc

    def initialize_stable_lock(self, authorization_id: str) -> StableLockIdentity:
        self._require_temp_root()
        ensure_private_directory(self.root)
        path, _ = self._paths(authorization_id)
        try:
            fd = open_regular(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except BackupFsError as exc:
            raise AuthorizationError("BACKUP_AUTHORIZATION_ALREADY_EXISTS") from exc
        try:
            os.fsync(fd)
            info = os.fstat(fd)
        finally:
            os.close(fd)
        fsync_directory(path.parent)
        return StableLockIdentity(
            resolved_device=info.st_dev,
            resolved_inode=info.st_ino,
        )

    def _validate_lock_identity(
        self,
        authorization: ProductionAuthorization,
        lock_identities: dict[str, StableLockIdentity],
    ) -> None:
        if (
            lock_identities.get(authorization.authorization_id)
            != authorization.stable_lock_identity
        ):
            raise AuthorizationError("BACKUP_AUTHORIZATION_LOCK_INVALID")

    def _paths(self, authorization_id: str) -> tuple[Path, Path]:
        if not OPAQUE_ID_PATTERN.fullmatch(authorization_id):
            raise AuthorizationError("BACKUP_AUTHORIZATION_INVALID")
        return (
            self.root / f"{authorization_id}.lock",
            self.root / f"{authorization_id}.json",
        )

    def _validate_nonce(self, value: ProductionAuthorization, nonce: str) -> None:
        if not isinstance(nonce, str) or not hmac.compare_digest(
            value.authorization_nonce, nonce
        ):
            raise AuthorizationError("BACKUP_AUTHORIZATION_NONCE_INVALID")

    def _validate_consumable(
        self,
        value: ProductionAuthorization,
        nonce: str,
        now: datetime,
    ) -> None:
        self._validate_nonce(value, nonce)
        if value.phase != "issued":
            raise AuthorizationError("BACKUP_AUTHORIZATION_ALREADY_CONSUMED")
        if now >= value.expires_at_utc.astimezone(UTC):
            raise AuthorizationError("BACKUP_AUTHORIZATION_EXPIRED")
        if value.backup_root_identity != self.root_identity():
            raise AuthorizationError("BACKUP_AUTHORIZATION_ROOT_MISMATCH")

    def _require_temp_root(self) -> None:
        try:
            _require_capability(
                self.backup_root,
                self.capability,
                "BACKUP_AUTHORIZATION_ISSUER_UNAVAILABLE",
            )
        except Exception as exc:
            if isinstance(exc, AuthorizationError):
                raise
            raise AuthorizationError("BACKUP_AUTHORIZATION_ISSUER_UNAVAILABLE") from exc
        validate_backup_root(self.backup_root)


class ProductionMutationCommandAdapter:
    """Production surface stays fail-closed until a later real issuer Gate."""

    @staticmethod
    def reject() -> AuthorizationResult:
        return AuthorizationResult(
            status="fail",
            authorization_id=None,
            phase="unknown",
            mutation_status="not_started",
            error_code="BACKUP_AUTHORIZATION_ISSUER_UNAVAILABLE",
        )


@contextmanager
def _stable_lock(path: Path) -> Iterator[StableLockIdentity]:
    fd: int | None = None
    try:
        fd = open_regular(path, os.O_RDWR)
        info = os.fstat(fd)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise AuthorizationError("BACKUP_AUTHORIZATION_LOCK_INVALID")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield StableLockIdentity(
            resolved_device=info.st_dev,
            resolved_inode=info.st_ino,
        )
    except AuthorizationError:
        raise
    except (BackupFsError, OSError) as exc:
        raise AuthorizationError("BACKUP_AUTHORIZATION_LOCK_INVALID") from exc
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _lock_identity(path: Path) -> StableLockIdentity:
    try:
        with _stable_lock(path) as identity:
            return identity
    except (BackupFsError, OSError) as exc:
        raise AuthorizationError("BACKUP_AUTHORIZATION_LOCK_INVALID") from exc


def _authorization_identity(value: ProductionAuthorization) -> str:
    return hashlib.sha256(
        _canonical_json(value.model_dump(mode="json"))
    ).hexdigest()


def _predecessor_ids(payload: AuthorizationPayload) -> tuple[str, ...]:
    if isinstance(payload, (PinReconcilePayload, RetentionResumePayload)):
        return (payload.predecessor_authorization_id,)
    return ()


def _validate_backup_ids(values: tuple[str, ...]) -> None:
    if len(values) != len(set(values)):
        raise ValueError("duplicate backup ids")
    if any(not BACKUP_ID_PATTERN.fullmatch(value) for value in values):
        raise ValueError("invalid backup id")


def _validate_opaque_id(value: str) -> None:
    if not OPAQUE_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid opaque id")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AuthorizationError("BACKUP_AUTHORIZATION_INVALID")
    return value.astimezone(UTC)


def _failure(authorization_id: str | None, exc: Exception) -> AuthorizationResult:
    safe_id = (
        authorization_id
        if authorization_id and OPAQUE_ID_PATTERN.fullmatch(authorization_id)
        else None
    )
    return AuthorizationResult(
        status="fail",
        authorization_id=safe_id,
        phase="unknown",
        mutation_status="not_started",
        error_code=getattr(exc, "error_code", "BACKUP_AUTHORIZATION_INVALID"),
    )

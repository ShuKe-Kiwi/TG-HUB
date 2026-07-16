"""Pure authorization and adapter contracts for P6-Deploy-4D-4C-1."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import pickle
import secrets
from pathlib import Path
from typing import Literal, Protocol

from app.deploy.backup_models import PgConnectionSpec
from app.deploy.production_recovery_postgres_models import (
    GeneratedDatabaseFacts,
    RehearsalCommand,
    TempPostgresRehearsalRecord,
)

_OBSERVATION_SECRET = object()
_CAPABILITY_SECRET = object()
_RESUME_EVIDENCE_SECRET = object()


class TempPostgresRehearsalError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class ServerRoleObservation:
    __slots__ = (
        "server_major",
        "server_identity_digest",
        "role_oid",
        "role_identity_digest",
        "_secret",
        "_sealed",
    )

    def __init__(
        self,
        *,
        server_major: int,
        server_identity_digest: str,
        role_oid: int,
        role_identity_digest: str,
        secret: object,
    ) -> None:
        if secret is not _OBSERVATION_SECRET:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
        if server_major < 1 or role_oid < 1:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_CONNECTION_UNSAFE")
        for digest in (server_identity_digest, role_identity_digest):
            if len(digest) != 64 or any(
                char not in "0123456789abcdef" for char in digest
            ):
                raise TempPostgresRehearsalError("TEMP_REHEARSAL_CONNECTION_UNSAFE")
        self.server_major = server_major
        self.server_identity_digest = server_identity_digest
        self.role_oid = role_oid
        self.role_identity_digest = role_identity_digest
        self._secret = secret
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("ServerRoleObservation is immutable")
        object.__setattr__(self, name, value)

    def __reduce__(self) -> object:
        raise pickle.PicklingError("ServerRoleObservation is not serializable")

    def __copy__(self) -> object:
        raise copy.Error("ServerRoleObservation is not copyable")

    def __deepcopy__(self, _memo: object) -> object:
        raise copy.Error("ServerRoleObservation is not copyable")

    def __repr__(self) -> str:
        return "ServerRoleObservation(<redacted>)"


class ObservationProvider(Protocol):
    def observe(self, spec: PgConnectionSpec) -> ServerRoleObservation: ...


class FakeObservationProvider:
    """Test-only provider. It performs no I/O and returns a controlled observation."""

    def __init__(self, *, server_major: int = 16, role_oid: int = 1000) -> None:
        self.server_major = server_major
        self.role_oid = role_oid
        self.calls = 0

    def observe(self, spec: PgConnectionSpec) -> ServerRoleObservation:
        validate_connection_policy(spec)
        self.calls += 1
        return ServerRoleObservation(
            server_major=self.server_major,
            server_identity_digest=hashlib.sha256(b"fake-server").hexdigest(),
            role_oid=self.role_oid,
            role_identity_digest=hashlib.sha256(b"fake-role").hexdigest(),
            secret=_OBSERVATION_SECRET,
        )


class RealObservationProvider:
    def observe(self, _spec: PgConnectionSpec) -> ServerRoleObservation:
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")


class TempPostgresAdapter(Protocol):
    def inspect(self, database: str) -> GeneratedDatabaseFacts: ...

    def create(
        self, capability: "TempPostgresRehearsalCapability", database: str
    ) -> None: ...

    def commit_identity(
        self, capability: "TempPostgresRehearsalCapability", database: str
    ) -> None: ...

    def restore(self, capability: "TempPostgresRehearsalCapability") -> None: ...

    def drop(
        self, capability: "TempPostgresRehearsalCapability", database: str
    ) -> None: ...


class FakeTempPostgresAdapter:
    """Stateful fake with the same scope fence as the later PostgreSQL adapter."""

    def __init__(self) -> None:
        self.facts: dict[str, GeneratedDatabaseFacts] = {}
        self.calls: list[str] = []

    def inspect(self, database: str) -> GeneratedDatabaseFacts:
        return self.facts.get(database, GeneratedDatabaseFacts(exists=False))

    def create(
        self, capability: "TempPostgresRehearsalCapability", database: str
    ) -> None:
        _require_database_action(capability, database, action="create")
        self.calls.append("create")
        self.facts[database] = GeneratedDatabaseFacts(
            exists=True, identity_matches=False, catalog_state="empty"
        )

    def commit_identity(
        self, capability: "TempPostgresRehearsalCapability", database: str
    ) -> None:
        _require_database_action(capability, database, action="commit_identity")
        current = self.inspect(database)
        if not current.exists:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_SOURCE_CREATE_FAILED")
        self.calls.append("commit_identity")
        self.facts[database] = current.model_copy(update={"identity_matches": True})

    def restore(self, capability: "TempPostgresRehearsalCapability") -> None:
        _require_database_action(
            capability, capability.replacement_database, action="restore"
        )
        current = self.inspect(capability.replacement_database)
        if not current.exists or not current.identity_matches:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_RESTORE_FAILED")
        self.calls.append("restore")
        self.facts[capability.replacement_database] = current.model_copy(
            update={"catalog_state": "complete"}
        )

    def drop(
        self, capability: "TempPostgresRehearsalCapability", database: str
    ) -> None:
        _require_database_action(capability, database, action="drop")
        self.calls.append("drop")
        self.facts.pop(database, None)


class TempPostgresRehearsalCapability:
    __slots__ = (
        "scope",
        "root",
        "root_device",
        "root_inode",
        "run_id",
        "source_database",
        "replacement_database",
        "source_token",
        "replacement_token",
        "server_identity_digest",
        "connection_identity_digest",
        "role_oid",
        "role_identity_digest",
        "_secret",
        "allowed_cleanup_targets",
        "_sealed",
    )

    def __init__(self, *, secret: object, **values: object) -> None:
        if secret is not _CAPABILITY_SECRET:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_secret", secret)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("TempPostgresRehearsalCapability is immutable")
        object.__setattr__(self, name, value)

    def __reduce__(self) -> object:
        raise pickle.PicklingError(
            "TempPostgresRehearsalCapability is not serializable"
        )

    def __copy__(self) -> object:
        raise copy.Error("TempPostgresRehearsalCapability is not copyable")

    def __deepcopy__(self, _memo: object) -> object:
        raise copy.Error("TempPostgresRehearsalCapability is not copyable")

    def __repr__(self) -> str:
        return f"TempPostgresRehearsalCapability(scope={self.scope!r}, <redacted>)"


class _ResumeRecordEvidence:
    __slots__ = ("record", "_secret", "_sealed")

    def __init__(self, record: TempPostgresRehearsalRecord, secret: object) -> None:
        if secret is not _RESUME_EVIDENCE_SECRET:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
        self.record = record
        self._secret = secret
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("resume evidence is immutable")
        object.__setattr__(self, name, value)


def _make_resume_evidence(record: TempPostgresRehearsalRecord) -> _ResumeRecordEvidence:
    """Called only after the durable store has locked and reread the record."""
    return _ResumeRecordEvidence(record, _RESUME_EVIDENCE_SECRET)


class TempPostgresCapabilityIssuer:
    def issue_initial(
        self,
        *,
        root: Path,
        spec: PgConnectionSpec,
        provider: ObservationProvider,
    ) -> TempPostgresRehearsalCapability:
        root, device, inode = validate_temp_root(root)
        observation = provider.observe(spec)
        _require_trusted_observation(observation)
        run_id = secrets.token_hex(16)
        suffix = secrets.token_hex(8)
        return self._issue(
            scope="initial",
            root=root,
            device=device,
            inode=inode,
            run_id=run_id,
            source_database=f"tg_hub_4c_source_{suffix}",
            replacement_database=f"tg_hub_4c_replacement_{suffix}",
            source_token=secrets.token_hex(32),
            replacement_token=secrets.token_hex(32),
            observation=observation,
            connection_identity_digest=_connection_digest(spec),
        )

    def issue_resume_cleanup(
        self,
        *,
        root: Path,
        evidence: _ResumeRecordEvidence,
        spec: PgConnectionSpec,
        provider: ObservationProvider,
    ) -> TempPostgresRehearsalCapability:
        root, device, inode = validate_temp_root(root, initialize=False)
        if evidence._secret is not _RESUME_EVIDENCE_SECRET:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
        record = evidence.record
        observation = provider.observe(spec)
        _require_trusted_observation(observation)
        if record.phase in {
            "planned",
            "source_create_started",
            "source_created",
            "source_identity_commit_started",
            "source_identity_committed",
            "dump_started",
            "dump_committed",
            "replacement_bound",
            "rehearsal_terminal",
            "manual_reconciliation_required",
        }:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
        if (
            record.server_identity_digest != observation.server_identity_digest
            or record.connection_identity_digest != _connection_digest(spec)
            or record.role_oid != observation.role_oid
            or record.role_identity_digest != observation.role_identity_digest
        ):
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_SERVER_IDENTITY_MISMATCH")
        return self._issue(
            scope="resume_cleanup",
            root=root,
            device=device,
            inode=inode,
            run_id=record.run_id,
            source_database=record.source_database_identity,
            replacement_database=record.replacement_database_identity,
            source_token=record.source_identity_token,
            replacement_token=record.replacement_identity_token,
            observation=observation,
            connection_identity_digest=record.connection_identity_digest,
            allowed_cleanup_targets=_cleanup_targets(record),
        )

    @staticmethod
    def _issue(
        *,
        scope: Literal["initial", "resume_cleanup"],
        root: Path,
        device: int,
        inode: int,
        run_id: str,
        source_database: str,
        replacement_database: str,
        source_token: str,
        replacement_token: str,
        observation: ServerRoleObservation,
        connection_identity_digest: str,
        allowed_cleanup_targets: tuple[str, ...] = (),
    ) -> TempPostgresRehearsalCapability:
        return TempPostgresRehearsalCapability(
            secret=_CAPABILITY_SECRET,
            scope=scope,
            root=root,
            root_device=device,
            root_inode=inode,
            run_id=run_id,
            source_database=source_database,
            replacement_database=replacement_database,
            source_token=source_token,
            replacement_token=replacement_token,
            server_identity_digest=observation.server_identity_digest,
            connection_identity_digest=connection_identity_digest,
            role_oid=observation.role_oid,
            role_identity_digest=observation.role_identity_digest,
            allowed_cleanup_targets=allowed_cleanup_targets,
        )


def validate_temp_root(
    path: Path, *, initialize: bool = True
) -> tuple[Path, int, int]:
    import tempfile

    try:
        candidate = path.expanduser()
        if candidate.is_symlink():
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
        root = candidate.resolve(strict=False)
        system_temp = Path(tempfile.gettempdir()).resolve()
        if system_temp != root and system_temp not in root.parents:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
        if initialize:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root.chmod(0o700)
        info = root.lstat()
    except TempPostgresRehearsalError:
        raise
    except OSError as exc:
        raise TempPostgresRehearsalError(
            "TEMP_REHEARSAL_NOT_AUTHORIZED"
        ) from exc
    if root.is_symlink() or not root.is_dir() or info.st_mode & 0o077:
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
    return root, info.st_dev, info.st_ino


def validate_connection_policy(spec: PgConnectionSpec) -> None:
    if spec.port is None or not spec.host or not spec.user or not spec.database:
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_CONNECTION_UNSAFE")
    try:
        loopback = ipaddress.ip_address(spec.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_CONNECTION_UNSAFE")


def validate_capability(capability: TempPostgresRehearsalCapability) -> None:
    try:
        info = capability.root.lstat()
    except OSError as exc:
        raise TempPostgresRehearsalError(
            "TEMP_REHEARSAL_NOT_AUTHORIZED"
        ) from exc
    if (
        capability._secret is not _CAPABILITY_SECRET
        or capability.root.is_symlink()
        or info.st_dev != capability.root_device
        or info.st_ino != capability.root_inode
    ):
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")


def build_restore_command(
    capability: TempPostgresRehearsalCapability,
    *,
    spec: PgConnectionSpec,
    pg_restore: Path,
    dump: Path,
) -> RehearsalCommand:
    validate_capability(capability)
    validate_connection_policy(spec)
    _require_connection_binding(capability, spec)
    if (
        capability.scope != "initial"
        or spec.database == capability.replacement_database
    ):
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
    if not pg_restore.is_absolute() or not dump.is_absolute():
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_CONNECTION_UNSAFE")
    env = _libpq_env(
        spec,
        database=capability.replacement_database,
        appname="tg-hub-4c-pg-restore",
    )
    return RehearsalCommand(
        argv=(
            str(pg_restore),
            f"--dbname={capability.replacement_database}",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            str(dump),
        ),
        env=env,
    )


def build_dump_command(
    capability: TempPostgresRehearsalCapability,
    *,
    spec: PgConnectionSpec,
    pg_dump: Path,
    dump: Path,
) -> RehearsalCommand:
    validate_capability(capability)
    validate_connection_policy(spec)
    _require_connection_binding(capability, spec)
    if (
        capability.scope != "initial"
        or not pg_dump.is_absolute()
        or not dump.is_absolute()
    ):
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
    env = _libpq_env(
        spec, database=capability.source_database, appname="tg-hub-4c-pg-dump"
    )
    return RehearsalCommand(
        argv=(
            str(pg_dump),
            "--format=custom",
            f"--file={dump}",
            capability.source_database,
        ),
        env=env,
    )


def switch_database_component(
    capability: TempPostgresRehearsalCapability,
    *,
    spec: PgConnectionSpec,
    target: Literal["source", "replacement"],
) -> PgConnectionSpec:
    validate_capability(capability)
    validate_connection_policy(spec)
    _require_connection_binding(capability, spec)
    database = (
        capability.source_database
        if target == "source"
        else capability.replacement_database
    )
    return spec.model_copy(update={"database": database})


def _libpq_env(
    spec: PgConnectionSpec, *, database: str, appname: str
) -> dict[str, str]:
    env = {
        "PGHOST": spec.host,
        "PGPORT": str(spec.port),
        "PGUSER": spec.user,
        "PGDATABASE": database,
        "PGAPPNAME": appname,
    }
    if spec.password is not None:
        env["PGPASSWORD"] = spec.password
    return env


def _connection_digest(spec: PgConnectionSpec) -> str:
    payload = json.dumps(
        {
            "host": spec.host,
            "port": spec.port,
            "user": spec.user,
            "database": spec.database,
            "password": spec.password,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _require_connection_binding(
    capability: TempPostgresRehearsalCapability,
    spec: PgConnectionSpec,
) -> None:
    if capability.connection_identity_digest != _connection_digest(spec):
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_CONNECTION_UNSAFE")


def _require_trusted_observation(observation: ServerRoleObservation) -> None:
    if observation._secret is not _OBSERVATION_SECRET:
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")


def _require_database_action(
    capability: TempPostgresRehearsalCapability,
    database: str,
    *,
    action: Literal["create", "commit_identity", "restore", "drop"],
) -> None:
    validate_capability(capability)
    allowed = {capability.source_database, capability.replacement_database}
    if database not in allowed:
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
    if action == "drop" and (
        capability.scope != "resume_cleanup"
        or database not in capability.allowed_cleanup_targets
    ):
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")
    if capability.scope == "resume_cleanup" and action != "drop":
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")


def _cleanup_targets(record: TempPostgresRehearsalRecord) -> tuple[str, ...]:
    if (
        record.phase == "workflow_terminal"
        and record.replacement_cleanup_status == "pending"
    ):
        return (record.replacement_database_identity,)
    if (
        record.phase == "source_cleanup_started"
        and record.replacement_cleanup_status == "completed"
        and record.source_cleanup_status == "pending"
    ):
        return (record.source_database_identity,)
    return ()
    if action == "restore" and database != capability.replacement_database:
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")


class ProductionPostgresRecoveryAdapter:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TempPostgresRehearsalError("PRODUCTION_RECOVERY_NOT_AUTHORIZED")

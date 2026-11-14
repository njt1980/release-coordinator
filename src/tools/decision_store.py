"""Append-only SQLite storage for release decision records.

Every record is placed in one SHA-256 hash chain (``core.decision.seal``):
the store reads the last record's hash and position and inserts the new
record after it inside one write transaction, so concurrent writers can't
fork the chain. Triggers make the table refuse UPDATE and DELETE, so the
application has no path to change history; ``verify`` recomputes every hash
and link, so a change made outside the application (editing the file) is
detected, not prevented.

Same SQLite settings as the review store: default rollback journal (no WAL:
network file shares lack shared memory), ``RC_SQLITE_VFS=unix-dotfile`` on
Azure Files, one connection per operation.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError, computed_field

from src.core import config
from src.core.decision import GENESIS_HASH, DecisionDraft, DecisionRecord, content_hash, seal
from src.tools.baseline_store import BaselineScope

_SCHEMA_VERSION: Final[int] = 1
_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS decision_records (
    sequence      INTEGER PRIMARY KEY,
    record_id     TEXT NOT NULL UNIQUE,
    tenant        TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    job_id        TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    record_hash   TEXT NOT NULL UNIQUE,
    record        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decision_records_by_job
    ON decision_records (tenant, project_id, job_id);
CREATE TRIGGER IF NOT EXISTS decision_records_no_update
    BEFORE UPDATE ON decision_records
    BEGIN SELECT RAISE(ABORT, 'decision records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decision_records_no_delete
    BEFORE DELETE ON decision_records
    BEGIN SELECT RAISE(ABORT, 'decision records are append-only'); END;
"""
_BUSY_TIMEOUT_SECONDS: Final[float] = 10.0


class DecisionStoreCorruptError(RuntimeError):
    """A stored row no longer validates, or the schema is not this version."""


class UnknownCorrectionError(ValueError):
    """A correction names a record that isn't a decision on the same assessment."""


class ChainReport(BaseModel):
    """The result of walking the whole chain: ``problems`` empty when intact."""

    records: int
    last_hash: str
    problems: list[str]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def intact(self) -> bool:
        """Whether every record's hash, link and position checked out."""
        return not self.problems


class DecisionStore:
    """Decision records for every project, in one SQLite file and one chain."""

    def __init__(self, path: Path, vfs: str | None = None) -> None:
        self.path = path
        self.vfs = config.SQLITE_VFS if vfs is None else vfs

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.vfs:
            target = f"{self.path.resolve().as_uri()}?vfs={self.vfs}"
            connection = sqlite3.connect(
                target, timeout=_BUSY_TIMEOUT_SECONDS, uri=True, isolation_level=None
            )
        else:
            connection = sqlite3.connect(
                self.path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None
            )
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            elif version != _SCHEMA_VERSION:
                raise DecisionStoreCorruptError(
                    f"{self.path}: schema version {version}, expected {_SCHEMA_VERSION}"
                )
            yield connection
        finally:
            connection.close()

    def append(self, draft: DecisionDraft) -> DecisionRecord:
        """Seal ``draft`` after the last record and store it.

        Raises:
            UnknownCorrectionError: if ``draft.corrects`` isn't a record of
                the same tenant, project and assessment job.
            DecisionStoreCorruptError: if the last stored row doesn't validate.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")  # one writer extends the chain
            try:
                if draft.corrects is not None:
                    self._check_corrects(connection, draft)
                last = connection.execute(
                    "SELECT sequence, record_hash FROM decision_records "
                    "ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                sequence, previous = (last[0] + 1, last[1]) if last else (1, GENESIS_HASH)
                record = seal(draft, sequence, previous)
                connection.execute(
                    "INSERT INTO decision_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.sequence,
                        record.record_id,
                        record.tenant,
                        record.project_id,
                        record.job_id,
                        record.previous_hash,
                        record.record_hash,
                        record.model_dump_json(),
                    ),
                )
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        return record

    def _check_corrects(self, connection: sqlite3.Connection, draft: DecisionDraft) -> None:
        row = connection.execute(
            "SELECT tenant, project_id, job_id FROM decision_records WHERE record_id = ?",
            (draft.corrects,),
        ).fetchone()
        if row is None or tuple(row) != (draft.tenant, draft.project_id, draft.job_id):
            raise UnknownCorrectionError(
                f"{draft.corrects} is not a decision on assessment {draft.job_id}"
            )

    def records(self, scope: BaselineScope, job_id: str | None = None) -> list[DecisionRecord]:
        """Every record of ``scope`` (one assessment's, if ``job_id``), oldest first.

        Raises:
            DecisionStoreCorruptError: if a row no longer validates or doesn't
                match the columns it is stored under.
        """
        if not self.path.exists():
            return []  # Nothing decided yet; reading never creates the file.
        query = (
            "SELECT sequence, record_id, tenant, project_id, job_id, record FROM decision_records "
            "WHERE tenant = ? AND project_id = ?"
        )
        parameters: list[str] = [scope.tenant, scope.project_id]
        if job_id is not None:
            query += " AND job_id = ?"
            parameters.append(job_id)
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY sequence", parameters).fetchall()
        return [self._parse(*row) for row in rows]

    def _parse(
        self, sequence: int, record_id: str, tenant: str, project_id: str, job_id: str, text: str
    ) -> DecisionRecord:
        try:
            record = DecisionRecord.model_validate_json(text)
        except ValidationError as error:
            raise DecisionStoreCorruptError(
                f"{self.path}: decision {record_id} does not validate: {error}"
            ) from error
        if (record.sequence, record.record_id, record.tenant, record.project_id, record.job_id) != (
            sequence, record_id, tenant, project_id, job_id
        ):
            raise DecisionStoreCorruptError(f"{self.path}: decision {record_id} does not match its row")
        return record

    def verify(self) -> ChainReport:
        """Walk the whole chain: every hash recomputed, every link and position checked."""
        if not self.path.exists():
            return ChainReport(records=0, last_hash=GENESIS_HASH, problems=[])
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, record_id, previous_hash, record_hash, record "
                "FROM decision_records ORDER BY sequence"
            ).fetchall()
        problems: list[str] = []
        previous = GENESIS_HASH
        for position, (sequence, record_id, previous_hash, record_hash, text) in enumerate(rows, 1):
            if sequence != position:
                problems.append(f"position {position}: sequence {sequence} (a record is missing)")
            if previous_hash != previous:
                problems.append(f"record {record_id}: does not link to the record before it")
            try:
                record = DecisionRecord.model_validate_json(text)
            except ValidationError:
                problems.append(f"record {record_id}: content does not match its hash or no longer validates")
            else:
                if (record.record_hash, record.previous_hash, record.sequence) != (
                    record_hash, previous_hash, sequence
                ) or content_hash(record) != record_hash:
                    problems.append(f"record {record_id}: stored columns disagree with its content")
            previous = record_hash
        return ChainReport(records=len(rows), last_hash=previous, problems=problems)


DEFAULT_DECISION_DB: Final[Path] = (
    config.DATA_ROOT / config.DECISIONS_DIR / config.DECISIONS_DB_NAME
)


def default_decision_store() -> DecisionStore:
    """Return the store at the project's ``decisions/decisions.sqlite3``."""
    return DecisionStore(DEFAULT_DECISION_DB)

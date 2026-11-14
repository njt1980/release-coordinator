"""Append-only SQLite storage for project dependency review answers.

Answers are never updated or deleted: a later answer for the same subject
supersedes an earlier one (``core.review.current_answers``), and the
history stays readable. Each row stores the validated answer as JSON next
to the columns it is queried by; rows are validated again when read, and a
row that no longer validates is reported, never skipped.

Like the decision records, answers form one SHA-256 hash chain (each row's
``record_hash`` covers its position, the previous row's hash and the
answer), triggers make the table refuse UPDATE and DELETE, and ``verify``
recomputes every hash and link, so a change made outside the application
is detected. Schema version 1 (no chain) is migrated on first use: its
answers are chained in the order they were given.

The database uses SQLite's default rollback journal, not WAL: WAL needs
shared memory, which network file shares (Azure Files) do not provide. On
such a share SQLite's byte-range locks fail too; ``RC_SQLITE_VFS=unix-dotfile``
locks with a lock file instead.
One connection is opened per operation, so the store holds no state
between calls and is safe to use from several threads.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError, computed_field

from src.core import config
from src.core.hashing import sha256_hex
from src.core.review import ReviewAnswer, ReviewSubject
from src.tools.baseline_store import BaselineScope

GENESIS_HASH: Final[str] = "0" * 64
_SCHEMA_VERSION: Final[int] = 2
# Executed one by one inside the migration's transaction (executescript
# would commit first).
_TABLE: Final[tuple[str, ...]] = (
    """CREATE TABLE review_answers (
        sequence      INTEGER PRIMARY KEY,
        answer_id     TEXT NOT NULL UNIQUE,
        tenant        TEXT NOT NULL,
        project_id    TEXT NOT NULL,
        subject_kind  TEXT NOT NULL,
        subject_key   TEXT NOT NULL,
        answered_at   TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        record_hash   TEXT NOT NULL UNIQUE,
        record        TEXT NOT NULL
    )""",
    """CREATE INDEX review_answers_by_subject
        ON review_answers (tenant, project_id, subject_kind, subject_key)""",
    """CREATE TRIGGER review_answers_no_update BEFORE UPDATE ON review_answers
        BEGIN SELECT RAISE(ABORT, 'review answers are append-only'); END""",
    """CREATE TRIGGER review_answers_no_delete BEFORE DELETE ON review_answers
        BEGIN SELECT RAISE(ABORT, 'review answers are append-only'); END""",
)
_BUSY_TIMEOUT_SECONDS: Final[float] = 10.0


class DuplicateAnswerError(ValueError):
    """An answer with this ``answer_id`` is already stored."""


class ReviewStoreCorruptError(RuntimeError):
    """A stored row no longer validates, or the schema is not this version."""


class ReviewChainReport(BaseModel):
    """The result of walking the answers' chain: ``problems`` empty when intact."""

    records: int
    last_hash: str
    problems: list[str]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def intact(self) -> bool:
        """Whether every answer's hash, link and position checked out."""
        return not self.problems


def chain_hash(sequence: int, previous_hash: str, record: str) -> str:
    """SHA-256 over an answer's chain position and its stored JSON."""
    return sha256_hex(
        {"sequence": sequence, "previous_hash": previous_hash, "record": json.loads(record)}
    )


class ReviewStore:
    """Review answers for every project, in one SQLite file."""

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
            self._ensure_schema(connection)
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == _SCHEMA_VERSION:
            return
        if version not in (0, 1):
            raise ReviewStoreCorruptError(
                f"{self.path}: schema version {version}, expected {_SCHEMA_VERSION}"
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Another connection may have migrated while this one waited.
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _TABLE:
                    connection.execute(statement)
            elif version == 1:
                self._migrate_v1(connection)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def _migrate_v1(self, connection: sqlite3.Connection) -> None:
        """Rebuild a version 1 table (no chain) as version 2, chaining answers in the order given."""
        rows = connection.execute(
            "SELECT answer_id, tenant, project_id, subject_kind, subject_key, answered_at, record "
            "FROM review_answers ORDER BY answered_at, answer_id"
        ).fetchall()
        connection.execute("ALTER TABLE review_answers RENAME TO review_answers_v1")
        connection.execute("DROP INDEX IF EXISTS review_answers_by_subject")
        for statement in _TABLE:
            connection.execute(statement)
        previous = GENESIS_HASH
        for sequence, row in enumerate(rows, 1):
            record_hash = chain_hash(sequence, previous, row[6])
            connection.execute(
                "INSERT INTO review_answers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sequence, *row[:6], previous, record_hash, row[6]),
            )
            previous = record_hash
        connection.execute("DROP TABLE review_answers_v1")

    def append(self, scope: BaselineScope, answer: ReviewAnswer) -> None:
        """Store ``answer`` for ``scope`` at the end of the chain.

        Raises:
            DuplicateAnswerError: if its ``answer_id`` is already stored.
        """
        record = answer.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")  # one writer extends the chain
            try:
                last = connection.execute(
                    "SELECT sequence, record_hash FROM review_answers ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                sequence, previous = (last[0] + 1, last[1]) if last else (1, GENESIS_HASH)
                connection.execute(
                    "INSERT INTO review_answers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sequence,
                        answer.answer_id,
                        scope.tenant,
                        scope.project_id,
                        answer.subject.kind.value,
                        answer.subject.key,
                        answer.answered_at.isoformat(),
                        previous,
                        chain_hash(sequence, previous, record),
                        record,
                    ),
                )
            except sqlite3.IntegrityError as error:
                connection.execute("ROLLBACK")
                raise DuplicateAnswerError(
                    f"answer {answer.answer_id} is already stored"
                ) from error
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def answers(
        self, scope: BaselineScope, subject: ReviewSubject | None = None
    ) -> list[ReviewAnswer]:
        """Every answer for ``scope`` (only ``subject``'s, if given), oldest
        first.

        Raises:
            ReviewStoreCorruptError: if a stored row no longer validates, its
                hash doesn't match, or it was stored under another scope or
                subject than it names.
        """
        if not self.path.exists():
            return []  # Nothing answered yet; reading never creates the file.
        query = (
            "SELECT answer_id, subject_kind, subject_key, sequence, previous_hash, record_hash, "
            "record FROM review_answers WHERE tenant = ? AND project_id = ?"
        )
        parameters: list[str] = [scope.tenant, scope.project_id]
        if subject is not None:
            query += " AND subject_kind = ? AND subject_key = ?"
            parameters += [subject.kind.value, subject.key]
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        answers = [self._parse(*row) for row in rows]
        return sorted(answers, key=lambda answer: (answer.answered_at, answer.answer_id))

    def _parse(
        self,
        answer_id: str,
        subject_kind: str,
        subject_key: str,
        sequence: int,
        previous_hash: str,
        record_hash: str,
        record: str,
    ) -> ReviewAnswer:
        try:
            answer = ReviewAnswer.model_validate_json(record)
            intact = chain_hash(sequence, previous_hash, record) == record_hash
        except (ValidationError, ValueError) as error:
            raise ReviewStoreCorruptError(
                f"{self.path}: answer {answer_id} does not validate: {error}"
            ) from error
        if not intact:
            raise ReviewStoreCorruptError(
                f"{self.path}: answer {answer_id} does not match its hash"
            )
        if (answer.answer_id, answer.subject.kind.value, answer.subject.key) != (
            answer_id,
            subject_kind,
            subject_key,
        ):
            raise ReviewStoreCorruptError(
                f"{self.path}: answer {answer_id} does not match its row"
            )
        return answer

    def verify(self) -> ReviewChainReport:
        """Walk the whole chain: every hash recomputed, every link and position checked."""
        if not self.path.exists():
            return ReviewChainReport(records=0, last_hash=GENESIS_HASH, problems=[])
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, answer_id, previous_hash, record_hash, record "
                "FROM review_answers ORDER BY sequence"
            ).fetchall()
        problems: list[str] = []
        previous = GENESIS_HASH
        for position, (sequence, answer_id, previous_hash, record_hash, record) in enumerate(rows, 1):
            if sequence != position:
                problems.append(f"position {position}: sequence {sequence} (an answer is missing)")
            if previous_hash != previous:
                problems.append(f"answer {answer_id}: does not link to the answer before it")
            try:
                intact = chain_hash(sequence, previous_hash, record) == record_hash
            except ValueError:
                intact = False
            if not intact:
                problems.append(f"answer {answer_id}: content does not match its hash")
            previous = record_hash
        return ReviewChainReport(records=len(rows), last_hash=previous, problems=problems)


DEFAULT_REVIEW_DB: Final[Path] = (
    config.DATA_ROOT / config.REVIEWS_DIR / config.REVIEWS_DB_NAME
)


def default_review_store() -> ReviewStore:
    """Return the store at the project's ``reviews/reviews.sqlite3``."""
    return ReviewStore(DEFAULT_REVIEW_DB)

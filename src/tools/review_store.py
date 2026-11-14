"""Append-only SQLite storage for project dependency review answers.

Answers are never updated or deleted: a later answer for the same subject
supersedes an earlier one (``core.review.current_answers``), and the
history stays readable. Each row stores the validated answer as JSON next
to the columns it is queried by; rows are validated again when read, and a
row that no longer validates is reported, never skipped.

The database uses SQLite's default rollback journal, not WAL: WAL needs
shared memory, which network file shares (Azure Files) do not provide.
One connection is opened per operation, so the store holds no state
between calls and is safe to use from several threads.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from src.core import config
from src.core.review import ReviewAnswer, ReviewSubject
from src.tools.baseline_store import BaselineScope

_SCHEMA_VERSION: Final[int] = 1
_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS review_answers (
    answer_id    TEXT PRIMARY KEY,
    tenant       TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_key  TEXT NOT NULL,
    answered_at  TEXT NOT NULL,
    record       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_answers_by_subject
    ON review_answers (tenant, project_id, subject_kind, subject_key);
"""
_BUSY_TIMEOUT_SECONDS: Final[float] = 10.0


class DuplicateAnswerError(ValueError):
    """An answer with this ``answer_id`` is already stored."""


class ReviewStoreCorruptError(RuntimeError):
    """A stored row no longer validates, or the schema is not this version."""


class ReviewStore:
    """Review answers for every project, in one SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_SECONDS)
        try:
            with connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    connection.executescript(_SCHEMA)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                elif version != _SCHEMA_VERSION:
                    raise ReviewStoreCorruptError(
                        f"{self.path}: schema version {version}, expected "
                        f"{_SCHEMA_VERSION}"
                    )
            with connection:
                yield connection
        finally:
            connection.close()

    def append(self, scope: BaselineScope, answer: ReviewAnswer) -> None:
        """Store ``answer`` for ``scope``.

        Raises:
            DuplicateAnswerError: if its ``answer_id`` is already stored.
        """
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO review_answers VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        answer.answer_id,
                        scope.tenant,
                        scope.project_id,
                        answer.subject.kind.value,
                        answer.subject.key,
                        answer.answered_at.isoformat(),
                        answer.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateAnswerError(
                f"answer {answer.answer_id} is already stored"
            ) from error

    def answers(
        self, scope: BaselineScope, subject: ReviewSubject | None = None
    ) -> list[ReviewAnswer]:
        """Every answer for ``scope`` (only ``subject``'s, if given), oldest
        first.

        Raises:
            ReviewStoreCorruptError: if a stored row no longer validates or
                was stored under another scope or subject than it names.
        """
        query = (
            "SELECT answer_id, subject_kind, subject_key, record FROM review_answers "
            "WHERE tenant = ? AND project_id = ?"
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
        self, answer_id: str, subject_kind: str, subject_key: str, record: str
    ) -> ReviewAnswer:
        try:
            answer = ReviewAnswer.model_validate_json(record)
        except ValidationError as error:
            raise ReviewStoreCorruptError(
                f"{self.path}: answer {answer_id} does not validate: {error}"
            ) from error
        if (answer.answer_id, answer.subject.kind.value, answer.subject.key) != (
            answer_id,
            subject_kind,
            subject_key,
        ):
            raise ReviewStoreCorruptError(
                f"{self.path}: answer {answer_id} does not match its row"
            )
        return answer


DEFAULT_REVIEW_DB: Final[Path] = (
    Path(__file__).resolve().parents[2] / config.REVIEWS_DIR / config.REVIEWS_DB_NAME
)


def default_review_store() -> ReviewStore:
    """Return the store at the project's ``reviews/reviews.sqlite3``."""
    return ReviewStore(DEFAULT_REVIEW_DB)

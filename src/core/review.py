"""Records of a coordinator's project dependency review.

Once per project, a release coordinator reviews the dependency map
discovery found: agrees with or disagrees with a dependency, adds a
missing one, answers a question the platform could not resolve, or links
two names of one service. Each answer is a ``ReviewAnswer``. Answers are
append-only history; the newest answer for a subject is the one that
counts (``current_answers``).

Every answer except an identity link is anchored to the evidence it was
given on: a repository commit and the files the answer rests on. When a
newer commit changes an anchored file, the answer needs re-review and no
longer counts (``answer_freshness``). When that cannot be proven either
way (no comparison, history rewritten, repository no longer scanned), the
answer also needs re-review: an answer is never trusted on evidence that
might have moved under it.

This module is pure: no storage, no network. ``src/tools/review_store``
stores answers; orchestration builds anchors and applies answers to a
baseline.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import Enum
from typing import Final, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from src.core.models import GIT_COMMIT_SHA_PATTERN

# Provenance label of a dependency a person declared (``DependencyEdge``).
HUMAN_DECLARED_PROVENANCE: Final[str] = "human_declared"

_EDGE_KEY_PREFIX: Final[str] = "edge:"
_EDGE_KEY_ARROW: Final[str] = "->"
_MAX_TEXT: Final[int] = 2000
_MAX_NAME: Final[int] = 200


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewSubjectKind(str, Enum):
    """What a review answer is about."""

    DEPENDENCY = "dependency"
    QUESTION = "question"
    IDENTITY = "identity"


class ReviewVerdict(str, Enum):
    """What the coordinator said.

    AGREE / DISAGREE: an edge discovery found is kept / removed.
    ADD: a missing edge is declared (subject is the declared edge).
    NOTHING_HIDDEN: a question's gap is accepted; nothing is behind it.
    CALLS: a question is answered with the dependencies behind it.
    LINK: an unlinked source name is the named runtime service.
    """

    AGREE = "agree"
    DISAGREE = "disagree"
    ADD = "add"
    NOTHING_HIDDEN = "nothing_hidden"
    CALLS = "calls"
    LINK = "link"


_VERDICTS_BY_KIND: Final[dict[ReviewSubjectKind, frozenset[ReviewVerdict]]] = {
    ReviewSubjectKind.DEPENDENCY: frozenset(
        {ReviewVerdict.AGREE, ReviewVerdict.DISAGREE, ReviewVerdict.ADD}
    ),
    ReviewSubjectKind.QUESTION: frozenset(
        {ReviewVerdict.NOTHING_HIDDEN, ReviewVerdict.CALLS}
    ),
    ReviewSubjectKind.IDENTITY: frozenset({ReviewVerdict.LINK}),
}
_REASON_REQUIRED: Final[frozenset[ReviewVerdict]] = frozenset(
    {
        ReviewVerdict.DISAGREE,
        ReviewVerdict.ADD,
        ReviewVerdict.NOTHING_HIDDEN,
        ReviewVerdict.CALLS,
    }
)
_DECLARES: Final[frozenset[ReviewVerdict]] = frozenset(
    {ReviewVerdict.ADD, ReviewVerdict.CALLS}
)


def edge_key(source_id: str, target_id: str) -> str:
    """Subject key of the dependency ``source_id`` -> ``target_id``."""
    return f"{_EDGE_KEY_PREFIX}{source_id}{_EDGE_KEY_ARROW}{target_id}"


class ReviewSubject(_Frozen):
    """The thing an answer is about.

    ``key`` is ``edge_key(...)`` for a dependency, the issue ``root_key``
    for a question (so one answer covers every issue with that cause), and
    ``issue_keys.identity_key(source_name)`` for an identity.
    """

    kind: ReviewSubjectKind
    key: str = Field(min_length=1, max_length=_MAX_TEXT)

    @model_validator(mode="after")
    def _dependency_keys_are_edges(self) -> Self:
        is_edge = self.key.startswith(_EDGE_KEY_PREFIX) and _EDGE_KEY_ARROW in self.key
        if (self.kind is ReviewSubjectKind.DEPENDENCY) != is_edge:
            raise ValueError(
                "a dependency subject key, and only one, must be an edge key: "
                f"{self.key!r}"
            )
        return self


def _valid_relative_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError(f"anchored path must be repository-relative: {path!r}")
    segments = path.removesuffix("/").split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"anchored path has an empty, '.' or '..' segment: {path!r}")
    return path


class ReviewAnchor(_Frozen):
    """Evidence one answer rests on, in one repository.

    ``paths`` are repository-relative files, or directories ending in
    ``/`` (a service root: any change below it counts). Matching is exact
    and case-sensitive, as in git.
    """

    repository: str = Field(min_length=1, max_length=_MAX_NAME)
    commit_sha: str = Field(pattern=GIT_COMMIT_SHA_PATTERN)
    paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("paths")
    @classmethod
    def _valid_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({_valid_relative_path(path) for path in paths}))

    def touched_by(self, changed_paths: Iterable[str]) -> list[str]:
        """The changed paths that fall on this anchor, sorted."""
        files = {path for path in self.paths if not path.endswith("/")}
        directories = [path for path in self.paths if path.endswith("/")]
        return sorted(
            {
                changed
                for changed in (path.lstrip("/") for path in changed_paths)
                if changed in files
                or any(changed.startswith(directory) for directory in directories)
            }
        )


class DeclaredDependency(_Frozen):
    """A dependency a coordinator declared (provenance HUMAN_DECLARED)."""

    source_id: str = Field(min_length=1, max_length=_MAX_TEXT)
    target_id: str = Field(min_length=1, max_length=_MAX_TEXT)
    protocol: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME)

    @model_validator(mode="after")
    def _not_a_self_call(self) -> Self:
        if self.source_id == self.target_id:
            raise ValueError(f"a service does not depend on itself: {self.source_id}")
        return self

    @property
    def key(self) -> str:
        return edge_key(self.source_id, self.target_id)


def _stripped(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


class ReviewAnswer(_Frozen):
    """One answer a coordinator gave, on the baseline ``snapshot_id``.

    ``reason`` is required to disagree, add, or answer a question (the
    "how do you know" a later reader needs). ``declared`` holds exactly the
    added edge for ADD, and at least one edge for CALLS; it is empty
    otherwise. ``linked_runtime_name`` is set exactly for LINK. Anchors
    are required except for LINK, which rests on names, not files.
    """

    answer_id: str = Field(min_length=1, max_length=_MAX_NAME)
    subject: ReviewSubject
    verdict: ReviewVerdict
    reviewer: str = Field(min_length=1, max_length=_MAX_NAME)
    answered_at: AwareDatetime
    snapshot_id: str = Field(min_length=1, max_length=_MAX_NAME)
    reason: str | None = Field(default=None, max_length=_MAX_TEXT)
    declared: tuple[DeclaredDependency, ...] = ()
    linked_runtime_name: str | None = Field(default=None, max_length=_MAX_NAME)
    anchors: tuple[ReviewAnchor, ...] = ()

    @field_validator("reviewer", mode="before")
    @classmethod
    def _strip_reviewer(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("reason", "linked_runtime_name", mode="before")
    @classmethod
    def _strip_optional(cls, value: object) -> object:
        return _stripped(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        verdict = self.verdict
        if verdict not in _VERDICTS_BY_KIND[self.subject.kind]:
            raise ValueError(
                f"{verdict.value} is not an answer to a {self.subject.kind.value}"
            )
        if verdict in _REASON_REQUIRED and self.reason is None:
            raise ValueError(f"{verdict.value} requires a reason")
        if verdict in _DECLARES:
            if not self.declared:
                raise ValueError(f"{verdict.value} requires declared dependencies")
            keys = [dependency.key for dependency in self.declared]
            if len(set(keys)) != len(keys):
                raise ValueError("declared dependencies must be unique")
        elif self.declared:
            raise ValueError(f"{verdict.value} declares no dependencies")
        if verdict is ReviewVerdict.ADD and (
            len(self.declared) != 1 or self.declared[0].key != self.subject.key
        ):
            raise ValueError("add declares exactly the dependency it is about")
        if (verdict is ReviewVerdict.LINK) != (self.linked_runtime_name is not None):
            raise ValueError("linked_runtime_name is set exactly for link")
        if verdict is not ReviewVerdict.LINK and not self.anchors:
            raise ValueError(f"{verdict.value} requires anchors")
        repositories = [anchor.repository for anchor in self.anchors]
        if len(set(repositories)) != len(repositories):
            raise ValueError("anchors must list each repository once")
        return self


def current_answers(answers: Iterable[ReviewAnswer]) -> dict[ReviewSubject, ReviewAnswer]:
    """The newest answer per subject (ties broken by ``answer_id``)."""
    newest: dict[ReviewSubject, ReviewAnswer] = {}
    for answer in answers:
        held = newest.get(answer.subject)
        if held is None or (answer.answered_at, answer.answer_id) > (
            held.answered_at,
            held.answer_id,
        ):
            newest[answer.subject] = answer
    return newest


class RepositoryChanges(_Frozen):
    """What changed in one repository between two commits.

    ``changed_paths`` are repository-relative, from the merge base of
    ``from_commit`` and ``to_commit`` to ``to_commit``; they describe the
    history between the two only when ``from_is_ancestor``.
    """

    repository: str = Field(min_length=1)
    from_commit: str = Field(pattern=GIT_COMMIT_SHA_PATTERN)
    to_commit: str = Field(pattern=GIT_COMMIT_SHA_PATTERN)
    from_is_ancestor: bool
    changed_paths: tuple[str, ...] = ()


class StaleReason(_Frozen):
    """Why one anchor no longer holds."""

    repository: str
    detail: str = Field(min_length=1)
    changed_paths: tuple[str, ...] = ()


class AnswerFreshness(_Frozen):
    """Whether an answer still counts against the current commits."""

    answer_id: str
    stale: tuple[StaleReason, ...] = ()
    checked_at: datetime | None = None

    @property
    def needs_rereview(self) -> bool:
        return bool(self.stale)


def _short(sha: str) -> str:
    return sha[:8]


def _anchor_staleness(
    anchor: ReviewAnchor,
    current_commits: Mapping[str, str],
    changes: Sequence[RepositoryChanges],
) -> StaleReason | None:
    repository = anchor.repository
    current = current_commits.get(repository)
    if current is None:
        return StaleReason(
            repository=repository,
            detail=f"{repository} is not in the current baseline",
        )
    if current == anchor.commit_sha:
        return None
    comparison = next(
        (
            change
            for change in changes
            if change.repository == repository
            and change.from_commit == anchor.commit_sha
            and change.to_commit == current
        ),
        None,
    )
    if comparison is None:
        return StaleReason(
            repository=repository,
            detail=(
                f"changes in {repository} from {_short(anchor.commit_sha)} to "
                f"{_short(current)} are unknown"
            ),
        )
    if not comparison.from_is_ancestor:
        return StaleReason(
            repository=repository,
            detail=(
                f"{repository} history was rewritten: {_short(anchor.commit_sha)} "
                f"is not an ancestor of {_short(current)}"
            ),
        )
    touched = anchor.touched_by(comparison.changed_paths)
    if touched:
        return StaleReason(
            repository=repository,
            detail=(
                f"{len(touched)} anchored file(s) in {repository} changed since "
                f"{_short(anchor.commit_sha)}"
            ),
            changed_paths=tuple(touched),
        )
    return None


def answer_freshness(
    answer: ReviewAnswer,
    current_commits: Mapping[str, str],
    changes: Sequence[RepositoryChanges],
    checked_at: datetime | None = None,
) -> AnswerFreshness:
    """Check ``answer``'s anchors against the current baseline's commits.

    Args:
        answer: the answer to check.
        current_commits: repository -> commit the current baseline scanned.
        changes: comparisons from anchor commits to current commits.
        checked_at: when the check ran, recorded on the result.
    """
    stale = tuple(
        reason
        for anchor in answer.anchors
        if (reason := _anchor_staleness(anchor, current_commits, changes)) is not None
    )
    return AnswerFreshness(answer_id=answer.answer_id, stale=stale, checked_at=checked_at)

"""The review queue: what a person should look at next, weakest evidence first.

Every unanswered item of a project's dependency review, ordered so that a
reviewer's time goes where the platform is least sure:

1. answers that stopped counting because their evidence changed;
2. open questions (something the platform could not see past);
3. dependencies whose evidence is weak (a default value, an unused
   environment variable, a person's word only);
4. dependencies with a single source of evidence (code only, telemetry only);
5. dependencies two or more sources agree on.

Within a level, items touching more critical services (see
``orchestration.inventory``) come first: the most critical service each
touches is compared, then the next. Each item names its owners, from
the policy's ``service_owners``: a dependency's caller owns it (its code or
configuration makes the call), a question belongs to the services it names.
"""

from collections.abc import Callable, Mapping
from typing import Final, Literal

from pydantic import BaseModel, Field

from src.core.models import DiscoveryMethod, ServiceIdentity
from src.orchestration.review import DependencyRow, ProjectReview, QuestionRow, ReviewStatus

_SOURCE_NAMES: Final[dict[DiscoveryMethod, str]] = {
    DiscoveryMethod.STATIC_CODE: "code",
    DiscoveryMethod.K8S_MANIFEST: "cluster",
    DiscoveryMethod.RUNTIME_TELEMETRY: "telemetry",
    DiscoveryMethod.HUMAN_DECLARED: "a person",
}


class QueueItem(BaseModel):
    """One thing to review, with why it is where it is in the queue."""

    position: int = Field(ge=1)
    key: str
    kind: Literal["question", "dependency"]
    title: str
    level: int = Field(ge=1, le=5)
    why: str
    owners: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)


def _short(service_id: str) -> str:
    return service_id.rsplit(":", 1)[-1]


def _namespaced(service_id: str) -> str:
    try:
        identity = ServiceIdentity.parse(service_id)
    except ValueError:
        return service_id
    return f"{identity.namespace}/{identity.service_name}"


# How a question's subject key reads in a title.
_SUBJECT_KINDS: Final[dict[str, str]] = {"env": "variable", "host": "address", "ip": "IP address"}


def _question_title(row: QuestionRow) -> str:
    """``<what> in <services>``: the subject key, readable, and who it concerns."""
    kind, _, name = row.subject.key.partition(":")
    what = f"{_SUBJECT_KINDS[kind]} {name}" if kind in _SUBJECT_KINDS and name else row.subject.key
    services = [_short(s) for s in row.services]
    shown = ", ".join(services[:3]) + (f" and {len(services) - 3} more" if len(services) > 3 else "")
    return f"Unresolved {what} in {shown}" if shown else f"Unresolved {what}"


def _dependency_level(row: DependencyRow) -> tuple[int, str]:
    if row.status is ReviewStatus.NEEDS_REREVIEW:
        reasons = "; ".join(reason.detail for reason in row.stale) or "its evidence changed"
        return 1, f"Its earlier answer stopped counting: {reasons}."
    if row.weak:
        return 3, f"Weak evidence: {row.weak}."
    sources = sorted({_SOURCE_NAMES.get(m, m.value) for m in row.discovery_methods})
    if len(sources) == 1:
        return 4, f"One source only: {sources[0]}."
    return 5, f"{' and '.join(sources)} agree."


def _question_level(row: QuestionRow) -> tuple[int, str]:
    if row.status is ReviewStatus.NEEDS_REREVIEW:
        return 1, "Its earlier answer stopped counting: the files it rested on changed."
    return 2, f"The platform couldn't see past this ({row.code.value})."


def review_queue(
    review: ProjectReview,
    criticality: Mapping[str, int],
    owner_of: Callable[[str], str | None],
) -> list[QueueItem]:
    """Every OPEN or NEEDS_REREVIEW item of ``review``, weakest evidence first.

    ``criticality`` maps service IDs to their inventory rank (1 = most
    critical); ``owner_of`` maps ``<namespace>/<name>`` to an owner.
    """
    waiting = (ReviewStatus.OPEN, ReviewStatus.NEEDS_REREVIEW)
    unranked = len(criticality) + 1
    drafts: list[tuple[int, tuple[int, ...], str, QueueItem]] = []

    def ranks(services: list[str]) -> tuple[int, ...]:
        """The services' criticality ranks, most critical first (compared in order)."""
        return tuple(sorted(criticality.get(s, unranked) for s in services)) or (unranked,)

    def owners(services: list[str]) -> list[str]:
        return sorted({owner for s in services if (owner := owner_of(_namespaced(s)))})

    for dependency in review.dependencies:
        if dependency.status not in waiting:
            continue
        level, why = _dependency_level(dependency)
        services = [dependency.source_id, dependency.target_id]
        drafts.append((level, ranks(services), dependency.subject.key, QueueItem(
            position=1, key=dependency.subject.key, kind="dependency",
            title=f"{_short(dependency.source_id)} → {_short(dependency.target_id)}",
            level=level, why=why, owners=owners([dependency.source_id]), services=services,
        )))
    for question in review.questions:
        if question.status not in waiting:
            continue
        level, why = _question_level(question)
        drafts.append((level, ranks(question.services), question.subject.key, QueueItem(
            position=1, key=question.subject.key, kind="question",
            title=_question_title(question),
            level=level, why=why, owners=owners(question.services), services=question.services,
        )))
    drafts.sort(key=lambda draft: draft[:3])
    return [item.model_copy(update={"position": position}) for position, (*_, item) in enumerate(drafts, 1)]

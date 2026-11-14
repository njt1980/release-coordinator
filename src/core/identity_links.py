"""Link a service's source-code name to its runtime name.

The scanner names a service after its directory (``OrderProcessor`` ->
``orderprocessor``); Kubernetes and telemetry use the deployed name
(``order-processor``). Exact equality misses such pairs, leaving one
code-only and one runtime-only node for the same service.

Links are made only on evidence that preserves meaning:

- ``CANONICAL_EQUAL``: the names are equal after folding case and the
  separators ``-``, ``_`` and ``.`` (``fold``), and the folded name is
  unique on both sides (one source name, one runtime name).
- Declared facts (``IdentityFacts``), for a source name left unlinked:
  ``IMAGE_REPOSITORY`` (an image built from the source directory, per a
  Compose, skaffold or pipeline file, is what the runtime service runs),
  ``DECLARED_NAME`` (a Compose service, Aspire resource or Dockerfile OCI
  title label built from the source directory has the runtime service's
  name, or a name the runtime service declares), ``RUNTIME_DECLARED_NAME``
  (the runtime service's identity label, ``OTEL_SERVICE_NAME`` or OCI
  ``image.title`` annotation is the source name) and, weakest,
  ``IMAGE_SOURCE_REPOSITORY`` (the runtime service's OCI ``image.source``
  annotation names the repository the source service was scanned from,
  compared by repository name; in a repository holding several services it
  points at each of them, so the uniqueness rule links none). A link needs
  exactly one runtime candidate across all of them, claimed by no other
  source name. Facts tie identities only; they never create an edge.

Anything weaker (a near name, a shared token) is a ``suggestion`` only:
shown to the coordinator as a question, never used as evidence. A
declared link (``release-coord.yaml`` naming the service as deployed)
needs no linking: the names are already equal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Final

from pydantic import BaseModel, Field

_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[-_.]")
# Suggestions: at most this many single-character edits between folded names.
_SUGGESTION_DISTANCE: Final[int] = 2
_MAX_SUGGESTIONS: Final[int] = 3


class LinkEvidenceKind(str, Enum):
    """Why a source name and a runtime name are the same service."""

    CANONICAL_EQUAL = "CANONICAL_EQUAL"
    IMAGE_REPOSITORY = "IMAGE_REPOSITORY"
    DECLARED_NAME = "DECLARED_NAME"
    RUNTIME_DECLARED_NAME = "RUNTIME_DECLARED_NAME"
    IMAGE_SOURCE_REPOSITORY = "IMAGE_SOURCE_REPOSITORY"


# Strongest first: the evidence a link made from several facts reports.
_DECLARED_EVIDENCE_ORDER: Final[tuple[LinkEvidenceKind, ...]] = (
    LinkEvidenceKind.IMAGE_REPOSITORY,
    LinkEvidenceKind.DECLARED_NAME,
    LinkEvidenceKind.RUNTIME_DECLARED_NAME,
    LinkEvidenceKind.IMAGE_SOURCE_REPOSITORY,
)


class IdentityFacts(BaseModel, frozen=True):
    """Declared names and image repositories of one service (identity only).

    For a source service: names and images its build files declare. For a
    runtime service: names its workloads declare (identity labels,
    ``OTEL_SERVICE_NAME``) and the repositories of the images it runs.
    ``image_repositories`` are normalized (see ``image_repository``).
    ``source_repositories`` are repository names (see ``source_repository``):
    for a source service, the repository it was scanned from; for a runtime
    service, the ones its OCI ``image.source`` annotations name.
    """

    names: frozenset[str] = frozenset()
    image_repositories: frozenset[str] = frozenset()
    source_repositories: frozenset[str] = frozenset()


class IdentityLink(BaseModel, frozen=True):
    """A source-code service name tied to exactly one runtime name."""

    source_name: str = Field(min_length=1)
    runtime_name: str = Field(min_length=1)
    evidence: LinkEvidenceKind
    # The declared value that made the link (a name or image repository).
    detail: str | None = None

    def describe(self) -> str:
        text = f"{self.evidence.value}: {self.source_name} = {self.runtime_name}"
        return f"{text} (via {self.detail})" if self.detail else text


class LinkResult(BaseModel):
    """Links made, and suggestions for the source names left unlinked."""

    links: list[IdentityLink] = Field(default_factory=list)
    unlinked: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Unlinked source name -> runtime names suggested (never applied).",
    )


def fold(name: str) -> str:
    """Case- and separator-insensitive form of a service name."""
    return _SEPARATORS.sub("", name.strip().lower())


def _edit_distance(first: str, second: str, limit: int) -> int:
    """Levenshtein distance, or ``limit + 1`` once it exceeds ``limit``."""
    if abs(len(first) - len(second)) > limit:
        return limit + 1
    previous = list(range(len(second) + 1))
    for i, char in enumerate(first, start=1):
        current = [i]
        for j, other in enumerate(second, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char != other))
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def suggestions(source_name: str, runtime_names: Iterable[str]) -> list[str]:
    """Runtime names close to ``source_name``: a question, never a link.

    Close means a folded edit distance of at most two, or one folded name
    containing the other (``cart`` / ``cartservice``).
    """
    key = fold(source_name)
    scored: list[tuple[int, str]] = []
    for name in sorted(set(runtime_names)):
        other = fold(name)
        if not key or not other or other == key:
            continue
        distance = _edit_distance(key, other, _SUGGESTION_DISTANCE)
        if distance <= _SUGGESTION_DISTANCE:
            scored.append((distance, name))
        elif key in other or other in key:
            scored.append((_SUGGESTION_DISTANCE + 1, name))
    return [name for _, name in sorted(scored)][:_MAX_SUGGESTIONS]


def image_repository(reference: str) -> str | None:
    """An image reference's repository path, without registry, tag or digest.

    ``myacr.azurecr.io/team/orders:1.2`` -> ``team/orders``. Leading path
    components holding an unexpanded variable (``${REGISTRY}/orders``) are
    dropped; None when nothing literal is left.
    """
    value = reference.strip().split("@", 1)[0]
    head, slash, last = value.rpartition("/")
    last = last.split(":", 1)[0]
    parts = [*(head.split("/") if slash else []), last]
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    while parts and ("$" in parts[0] or "{" in parts[0]):
        parts = parts[1:]
    if not parts or any(not part or "$" in part or "{" in part for part in parts):
        return None
    return "/".join(parts).lower()


def source_repository(reference: str) -> str | None:
    """A source repository URL's (or name's) repository name, lower-case.

    ``https://github.com/acme/Orders.git`` -> ``orders``;
    ``https://dev.azure.com/org/proj/_git/Orders`` -> ``orders``;
    ``Orders`` -> ``orders``. None when nothing literal is left.
    """
    value = reference.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    name = value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name.lower().endswith(".git"):
        name = name[: -len(".git")]
    if not name or "$" in name or "{" in name:
        return None
    return name.lower()


def _same_repository(first: str, second: str) -> bool:
    """Equal repository paths, or one a path suffix of the other
    (``orders`` / ``team/orders``: registries add namespaces)."""
    return first == second or first.endswith("/" + second) or second.endswith("/" + first)


def _declared_candidates(
    source_name: str,
    facts: IdentityFacts,
    runtimes: Mapping[str, IdentityFacts],
) -> dict[str, tuple[LinkEvidenceKind, str]]:
    """Runtime name -> strongest declared evidence tying it to ``source_name``."""
    found: dict[str, list[tuple[LinkEvidenceKind, str]]] = {}
    source_key = fold(source_name)
    declared = {fold(name): name for name in facts.names if fold(name)}
    for runtime_name, runtime in runtimes.items():
        hits = found.setdefault(runtime_name, [])
        for repository in sorted(facts.image_repositories):
            if any(_same_repository(repository, other) for other in runtime.image_repositories):
                hits.append((LinkEvidenceKind.IMAGE_REPOSITORY, repository))
        for key in sorted({fold(runtime_name), *(fold(n) for n in runtime.names)} & declared.keys()):
            hits.append((LinkEvidenceKind.DECLARED_NAME, declared[key]))
        for name in sorted(runtime.names):
            if source_key and fold(name) == source_key:
                hits.append((LinkEvidenceKind.RUNTIME_DECLARED_NAME, name))
        for repository in sorted(facts.source_repositories & runtime.source_repositories):
            hits.append((LinkEvidenceKind.IMAGE_SOURCE_REPOSITORY, repository))
    return {
        name: min(hits, key=lambda hit: _DECLARED_EVIDENCE_ORDER.index(hit[0]))
        for name, hits in found.items()
        if hits
    }


def link_identities(
    source_names: Iterable[str],
    runtime_names: Iterable[str],
    *,
    source_facts: Mapping[str, IdentityFacts] | None = None,
    runtime_facts: Mapping[str, IdentityFacts] | None = None,
) -> LinkResult:
    """Link source names to runtime names by canonical equality.

    ``source_names`` are code services with no exact runtime match;
    ``runtime_names`` the runtime services no source name matches exactly.
    A folded key shared by two source names, or by two runtime names, links
    nothing: equality must be unique on both sides.

    A source name canonical equality leaves unlinked is then linked by its
    declared facts (``source_facts``, against ``runtime_facts`` of the
    runtime names not already linked) when they point at exactly one
    runtime name that no other source name's facts point at. Conflicting
    or shared candidates link nothing and are offered as suggestions.
    """
    sources = sorted(set(source_names))
    runtimes = sorted(set(runtime_names))
    by_source: dict[str, list[str]] = {}
    by_runtime: dict[str, list[str]] = {}
    for name in sources:
        by_source.setdefault(fold(name), []).append(name)
    for name in runtimes:
        by_runtime.setdefault(fold(name), []).append(name)
    result = LinkResult()
    for name in sources:
        key = fold(name)
        matches = by_runtime.get(key, [])
        if key and len(matches) == 1 and len(by_source[key]) == 1:
            result.links.append(
                IdentityLink(
                    source_name=name,
                    runtime_name=matches[0],
                    evidence=LinkEvidenceKind.CANONICAL_EQUAL,
                )
            )
        else:
            result.unlinked[name] = suggestions(name, runtimes)
    if source_facts:
        _link_declared(result, runtimes, source_facts, runtime_facts or {})
    return result


def _link_declared(
    result: LinkResult,
    runtimes: list[str],
    source_facts: Mapping[str, IdentityFacts],
    runtime_facts: Mapping[str, IdentityFacts],
) -> None:
    """Link ``result``'s unlinked source names by declared facts, in place."""
    taken = {link.runtime_name for link in result.links}
    open_runtimes = {
        name: runtime_facts.get(name, IdentityFacts()) for name in runtimes if name not in taken
    }
    candidates = {
        name: _declared_candidates(name, source_facts.get(name, IdentityFacts()), open_runtimes)
        for name in sorted(result.unlinked)
    }
    claims: dict[str, int] = {}
    for found in candidates.values():
        for runtime_name in found:
            claims[runtime_name] = claims.get(runtime_name, 0) + 1
    for name, found in candidates.items():
        if len(found) == 1:
            runtime_name, (evidence, detail) = next(iter(found.items()))
            if claims[runtime_name] == 1:
                result.links.append(
                    IdentityLink(
                        source_name=name,
                        runtime_name=runtime_name,
                        evidence=evidence,
                        detail=detail,
                    )
                )
                del result.unlinked[name]
                continue
        if found:
            ordered = [*sorted(found), *result.unlinked[name]]
            result.unlinked[name] = list(dict.fromkeys(ordered))[:_MAX_SUGGESTIONS]

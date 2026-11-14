"""Link a service's source-code name to its runtime name.

The scanner names a service after its directory (``OrderProcessor`` ->
``orderprocessor``); Kubernetes and telemetry use the deployed name
(``order-processor``). Exact equality misses such pairs, leaving one
code-only and one runtime-only node for the same service.

Links are made only on evidence that preserves meaning:

- ``CANONICAL_EQUAL``: the names are equal after folding case and the
  separators ``-``, ``_`` and ``.`` (``fold``), and the folded name is
  unique on both sides (one source name, one runtime name).

Anything weaker (a near name, a shared token) is a ``suggestion`` only:
shown to the coordinator as a question, never used as evidence. A
declared link (``release-coord.yaml`` naming the service as deployed)
needs no linking: the names are already equal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
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


class IdentityLink(BaseModel, frozen=True):
    """A source-code service name tied to exactly one runtime name."""

    source_name: str = Field(min_length=1)
    runtime_name: str = Field(min_length=1)
    evidence: LinkEvidenceKind

    def describe(self) -> str:
        return f"{self.evidence.value}: {self.source_name} = {self.runtime_name}"


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


def link_identities(source_names: Iterable[str], runtime_names: Iterable[str]) -> LinkResult:
    """Link source names to runtime names by canonical equality.

    ``source_names`` are code services with no exact runtime match;
    ``runtime_names`` the runtime services no source name matches exactly.
    A folded key shared by two source names, or by two runtime names, links
    nothing: equality must be unique on both sides.
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
    return result

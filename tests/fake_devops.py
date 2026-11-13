"""A scripted stand-in for ``DevOpsExtractor`` in coordinator tests.

Commits form one line of history, ``history`` (oldest first): a commit is
an ancestor of every later one. ``branch`` commits fork from the first
commit of ``history``: they descend from it but not from later main-line
commits. A short SHA resolves to the full one it
prefixes. ``changes`` maps (base, target) full-SHA pairs to the paths that
range changes; ``head_references`` maps a service root to the references
the head declares.
"""

from typing import Any

from src.core.models import DiscoveryResult, ScannedRevision
from src.tools.devops_extractor import (
    BoundarySource,
    CommitComparison,
    EndpointReference,
    ReferenceKind,
    RepositoryScan,
    ServiceBoundary,
    ServiceScan,
)


def sha(prefix: str) -> str:
    """Return a full 40-character SHA starting with ``prefix``."""
    return prefix.lower().ljust(40, "0")


def url(service: str, endpoint: str, path: str = "main.go") -> EndpointReference:
    return EndpointReference(service_name=service, endpoint=endpoint, file_path=path)


def env(service: str, name: str, path: str = "main.go") -> EndpointReference:
    return EndpointReference(
        service_name=service, endpoint=name, file_path=path, kind=ReferenceKind.ENV_VAR
    )


class FakeDevOps:
    def __init__(
        self,
        history: list[str],
        changes: dict[tuple[str, str], list[str]] | None = None,
        head_references: dict[str, list[EndpointReference]] | None = None,
        unrelated: set[str] | None = None,
        fail_compare: bool = False,
        fail_scan: bool = False,
        missing_roots: set[str] | None = None,
        branch: list[str] | None = None,
    ) -> None:
        self.history = [sha(c) for c in history]
        self.branch = [sha(c) for c in branch or []]
        self.unrelated = {sha(c) for c in unrelated or set()}
        self.changes = {(sha(a), sha(b)): paths for (a, b), paths in (changes or {}).items()}
        self.head_references = head_references or {}
        self.fail_compare = fail_compare
        self.fail_scan = fail_scan
        self.missing_roots = missing_roots or set()
        self.scanned: list[tuple[ScannedRevision | None, set[str] | None]] = []

    def __call__(self, **_: Any) -> "FakeDevOps":
        """Stand in for the class: ``DevOpsExtractor(org_url=..., ...)``."""
        return self

    def _full(self, commit: str) -> str:
        commit = commit.lower()
        for known in [*self.history, *self.branch, *self.unrelated]:
            if known.startswith(commit):
                return known
        return sha(commit)

    def compare_commits(self, repo: str, base: str, target: str) -> DiscoveryResult[CommitComparison]:
        if self.fail_compare:
            return DiscoveryResult[CommitComparison].failed(f"{repo}: commit not found")
        base, target = self._full(base), self._full(target)
        ancestor = self._is_ancestor(base, target)
        return DiscoveryResult[CommitComparison].success(
            [
                CommitComparison(
                    repository=repo,
                    base=base,
                    target=target,
                    merge_bases=[base] if ancestor else [],
                    changed_paths=self.changes.get((base, target), []),
                )
            ]
        )

    def _is_ancestor(self, base: str, target: str) -> bool:
        lines = [self.history, self.branch]
        if any(base in line and target in line and line.index(base) <= line.index(target) for line in lines):
            return True
        return bool(self.history) and base == self.history[0] and target in self.branch

    def scan_repository(
        self, repo: str, commit: ScannedRevision | None = None, only_roots: set[str] | None = None
    ) -> DiscoveryResult[RepositoryScan]:
        self.scanned.append((commit, only_roots))
        if self.fail_scan:
            return DiscoveryResult[RepositoryScan].failed(f"{repo}: scan failed")
        assert commit is not None
        services = [
            ServiceScan(
                service=ServiceBoundary(
                    name=root.rsplit("/", 1)[-1] or "monorepo",
                    root=root,
                    source=BoundarySource.MARKER,
                ),
                references=self.head_references.get(root, []),
            )
            for root in sorted(only_roots or set())
            if root not in self.missing_roots
        ]
        return DiscoveryResult[RepositoryScan].success(
            [RepositoryScan(revision=commit, services=services)]
        )

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

from collections.abc import Iterable

from src.core.models import (
    ChannelUse,
    ContractLink,
    DiscoveryResult,
    RepositoryCoverage,
    ScannedRevision,
    ServiceCoverage,
    ServiceNode,
    UnscannedFile,
)
from src.tools.devops_extractor import (
    BoundarySource,
    CommitComparison,
    EndpointReference,
    ReferenceKind,
    RepositoryScan,
    ServiceBoundary,
    ServiceScan,
)


def coverage_for(
    services: Iterable[ServiceNode],
    commit_sha: str = "a" * 40,
    unscanned: dict[tuple[str, str], list[UnscannedFile]] | None = None,
) -> list[RepositoryCoverage]:
    """Full coverage records for every repository-scanned service, with
    ``unscanned`` (by repository and service root) marking files the scan
    couldn't read."""
    by_repository: dict[str, list[ServiceCoverage]] = {}
    for service in services:
        repository = service.metadata.get("repository")
        if repository is None:
            continue
        root = service.metadata.get("service_root", "")
        by_repository.setdefault(repository, []).append(
            ServiceCoverage(
                service_name=service.name,
                root=root,
                files_scanned=1,
                unscanned=(unscanned or {}).get((repository, root), []),
            )
        )
    return [
        RepositoryCoverage(repository=repository, commit_sha=commit_sha, services=records)
        for repository, records in by_repository.items()
    ]


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
        head_unscanned: dict[str, list[UnscannedFile]] | None = None,
        files: dict[tuple[str, str], str] | None = None,
        head_contracts: dict[str, list[ContractLink]] | None = None,
        head_channels: dict[str, list[ChannelUse]] | None = None,
    ) -> None:
        self.history = [sha(c) for c in history]
        self.branch = [sha(c) for c in branch or []]
        self.unrelated = {sha(c) for c in unrelated or set()}
        self.changes = {(sha(a), sha(b)): paths for (a, b), paths in (changes or {}).items()}
        self.head_references = head_references or {}
        self.fail_compare = fail_compare
        self.fail_scan = fail_scan
        self.missing_roots = missing_roots or set()
        self.head_unscanned = head_unscanned or {}
        self.head_contracts = head_contracts or {}
        self.head_channels = head_channels or {}
        # (path, commit) -> content; a missing key is a file absent there.
        self.files = {(path, sha(commit)): text for (path, commit), text in (files or {}).items()}
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

    def read_file(self, repo: str, path: str, commit: str) -> str | None:
        return self.files.get((path, self._full(commit)))

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
                coverage=ServiceCoverage(
                    service_name=root.rsplit("/", 1)[-1] or "monorepo",
                    root=root,
                    files_scanned=1,
                    unscanned=self.head_unscanned.get(root, []),
                    contracts=self.head_contracts.get(root, []),
                ),
                channel_uses=self.head_channels.get(root, []),
            )
            for root in sorted(only_roots or set())
            if root not in self.missing_roots
        ]
        return DiscoveryResult[RepositoryScan].success(
            [RepositoryScan(revision=commit, services=services)]
        )

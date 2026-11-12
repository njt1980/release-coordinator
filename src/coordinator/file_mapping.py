"""Map a pull request's changed files to registered services.

Static-code discovery records, for each service it finds, the repository it
lives in and its repository-relative root directory (``repository`` and
``service_root`` in ``ServiceNode.metadata``; ``""`` is the repository
root). A changed file belongs to the service in the same repository whose
root is the deepest directory containing the file, mirroring how discovery
assigned source files to services. Files no service owns, and files whose
deepest owner is ambiguous, are reported instead of guessed.
"""

from pydantic import BaseModel, Field

from src.core.models import (
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    KnowledgeGraph,
    ServiceNode,
)


class ChangedFileMapping(BaseModel):
    """Which registered services a pull request's changed files belong to.

    ``target_services`` are FQURIs sorted by ID. ``files_by_service`` maps
    each of them to its changed files in input order. ``unmapped_files``
    holds the files no single service owns.
    """

    target_services: list[str] = Field(default_factory=list)
    files_by_service: dict[str, list[str]] = Field(default_factory=dict)
    unmapped_files: list[str] = Field(default_factory=list)


def _segments(path: str) -> list[str] | None:
    """Split a repository-relative path, or return None if it escapes the repo."""
    segments = [s for s in path.strip().replace("\\", "/").split("/") if s and s != "."]
    if ".." in segments:
        return None
    return segments


def _service_roots(
    graph: KnowledgeGraph, repository: str
) -> list[tuple[list[str], ServiceNode]]:
    """Return ``(root segments, service)`` for services in ``repository``.

    Repository names compare case-insensitively, as Azure DevOps does.
    Services without a recorded root (for example those found only in
    Kubernetes, or baselines written before roots were recorded) are left
    out.
    """
    wanted = repository.strip().casefold()
    roots: list[tuple[list[str], ServiceNode]] = []
    for service in graph.services:
        repo = service.metadata.get(REPOSITORY_METADATA_KEY)
        root = service.metadata.get(SERVICE_ROOT_METADATA_KEY)
        if repo is None or root is None or repo.casefold() != wanted:
            continue
        segments = _segments(root)
        if segments is not None:
            roots.append((segments, service))
    return roots


def _owner(
    path: list[str], roots: list[tuple[list[str], ServiceNode]]
) -> str | None:
    """Return the ID of the single service with the deepest root containing ``path``."""
    owners: list[str] = []
    depth = -1
    for root, service in roots:
        if len(root) > len(path) or path[: len(root)] != root:
            continue
        if len(root) > depth:
            owners, depth = [service.service_id], len(root)
        elif len(root) == depth and service.service_id not in owners:
            owners.append(service.service_id)
    return owners[0] if len(owners) == 1 else None


def map_changed_files(
    graph: KnowledgeGraph, repository: str, changed_files: list[str]
) -> ChangedFileMapping:
    """Map ``changed_files`` in ``repository`` to the services that own them.

    Paths are repository-relative; leading ``/`` or ``./`` is ignored and
    paths containing ``..`` are never mapped. A file is owned by the service
    whose root is the deepest directory containing it; a tie between
    services is ambiguous and leaves the file unmapped.
    """
    roots = _service_roots(graph, repository)
    files_by_service: dict[str, list[str]] = {}
    unmapped: list[str] = []
    for changed_file in changed_files:
        segments = _segments(changed_file)
        owner = _owner(segments, roots) if segments else None
        if owner is None:
            unmapped.append(changed_file)
        else:
            files_by_service.setdefault(owner, []).append(changed_file)
    targets = sorted(files_by_service)
    return ChangedFileMapping(
        target_services=targets,
        files_by_service={service: files_by_service[service] for service in targets},
        unmapped_files=unmapped,
    )

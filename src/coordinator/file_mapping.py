"""Map a pull request's changed files to registered services.

Static-code discovery records, for each service it finds, the repository it
lives in and its repository-relative root directory (``repository`` and
``service_root`` in ``ServiceNode.metadata``; ``""`` is the repository
root). A changed file belongs to the service in the same repository whose
root is the deepest directory containing the file, mirroring how discovery
assigned source files to services. A file of a shared library (recorded in
the baseline's ``static_coverage``) whose root is deeper than any service's
belongs to every service consuming the library: its code ships in each.
Files no service owns, and files whose deepest owner is ambiguous, are
reported instead of guessed.
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


def normalize_changed_path(path: str) -> str:
    """Return a changed file's path as source control reports it: relative,
    ``/``-separated, without ``.`` segments. Paths escaping the repository
    are returned unchanged so they never match a real path."""
    segments = _segments(path)
    return "/".join(segments) if segments is not None else path


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


def _library_roots(
    graph: KnowledgeGraph, repository: str
) -> list[tuple[list[str], list[str]]]:
    """Return ``(library root segments, consumer service roots)`` in ``repository``."""
    wanted = repository.strip().casefold()
    libraries: list[tuple[list[str], list[str]]] = []
    for record in graph.static_coverage or []:
        if record.repository.casefold() != wanted:
            continue
        for library in record.libraries:
            segments = _segments(library.root)
            if segments is not None and library.consumers:
                libraries.append((segments, library.consumers))
    return libraries


def _deepest(path: list[str], roots: list[list[str]]) -> int:
    """Depth of the deepest root containing ``path`` (-1 for none)."""
    return max(
        (len(root) for root in roots if len(root) <= len(path) and path[: len(root)] == root),
        default=-1,
    )


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


def _owners(
    path: list[str],
    roots: list[tuple[list[str], ServiceNode]],
    libraries: list[tuple[list[str], list[str]]],
) -> tuple[list[str], bool]:
    """Return the services owning ``path`` and whether every owner was found.

    A library's consumers own the file when the library's root is deeper
    than every service root containing it; a consumer root that doesn't
    name exactly one registered service leaves the mapping incomplete.
    Otherwise the single deepest service owns it.
    """
    service_depth = _deepest(path, [root for root, _ in roots])
    containing = [
        (root, consumers)
        for root, consumers in libraries
        if len(root) <= len(path) and path[: len(root)] == root
    ]
    if containing:
        root, consumers = max(containing, key=lambda item: len(item[0]))
        if len(root) > service_depth:
            owners: set[str] = set()
            complete = True
            for consumer in consumers:
                segments = _segments(consumer) or []
                owner = _owner(segments, [r for r in roots if r[0] == segments])
                if owner is None:
                    complete = False
                else:
                    owners.add(owner)
            return sorted(owners), complete and bool(owners)
    owner = _owner(path, roots)
    return ([owner], True) if owner else ([], False)


def map_changed_files(
    graph: KnowledgeGraph, repository: str, changed_files: list[str]
) -> ChangedFileMapping:
    """Map ``changed_files`` in ``repository`` to the services that own them.

    Paths are repository-relative; leading ``/`` or ``./`` is ignored and
    paths containing ``..`` are never mapped. A file is owned by the service
    whose root is the deepest directory containing it, or by every consumer
    of the shared library it belongs to (see the module docstring); a tie
    between services is ambiguous and leaves the file unmapped. A library
    file with a consumer that isn't a registered service is mapped to the
    consumers that are and also listed as unmapped.
    """
    roots = _service_roots(graph, repository)
    libraries = _library_roots(graph, repository)
    files_by_service: dict[str, list[str]] = {}
    unmapped: list[str] = []
    for changed_file in changed_files:
        segments = _segments(changed_file)
        owners, complete = _owners(segments, roots, libraries) if segments else ([], False)
        if not complete:
            unmapped.append(changed_file)
        for owner in owners:
            files_by_service.setdefault(owner, []).append(changed_file)
    targets = sorted(files_by_service)
    return ChangedFileMapping(
        target_services=targets,
        files_by_service={service: files_by_service[service] for service in targets},
        unmapped_files=unmapped,
    )

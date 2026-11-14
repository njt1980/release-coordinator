"""Read-only clients for extracting Azure DevOps repository and pipeline data."""

import ipaddress
import logging
import os
import re
from enum import Enum
from typing import Any, Final, Self
from urllib.parse import urlsplit

import yaml

from azure.devops.connection import Connection
from azure.devops.released.git import (  # type: ignore[import-untyped]
    GitBaseVersionDescriptor,
    GitTargetVersionDescriptor,
    GitVersionDescriptor,
)
from azure.identity import DefaultAzureCredential
from msrest.authentication import BasicTokenAuthentication
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.models import DiscoveryResult, ScannedRevision
from src.core.env_names import is_endpoint_variable
from src.core.service_ids import env_var_reference, is_private_dns_host
from src.tools._timeout import (
    TIMEOUT_MESSAGE,
    VERIFY_TIMEOUT_SECONDS,
    run_with_timeout,
)


logger = logging.getLogger(__name__)
_AZURE_DEVOPS_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"
_TEXT_FILE_SUFFIXES = {
    ".cfg",
    ".conf",
    ".cs",
    ".env",
    ".go",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".php",
    ".properties",
    ".py",
    ".rb",
    ".sh",
    ".toml",
    ".ts",
    ".xml",
    ".yaml",
    ".yml",
}
_URL_PATTERN = re.compile(r"""https?://[^\s"'<>)}\]`]+""", re.IGNORECASE)
_EXCLUDED_DIRECTORIES = {
    ".git",
    "bower_components",
    "build",
    "dist",
    "docs",
    "node_modules",
    "target",
    "vendor",
}
_EXCLUDED_FILENAMES = {
    "composer.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}
# Optional repository-root manifest declaring service boundaries explicitly.
RELEASE_COORD_MANIFEST_PATH: Final[str] = "/release-coord.yaml"
# Files whose presence marks a directory as a potential service boundary when
# the repository has no manifest.
_SERVICE_MARKER_FILENAMES = {"dockerfile", "go.mod", "package.json"}
# Conventional grouping directories: their immediate children are scanned for
# service markers as if they were immediate children of the repository root.
_SERVICE_CONTAINER_DIRECTORIES = {"apps", "cmd", "services", "src"}
# A single DNS label: how one in-cluster service addresses another.
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Redacted placeholders such as x.x.x.x or *.*.*.* (not real addresses).
_IP_PLACEHOLDER_PATTERN = re.compile(r"^(?=.*[x*])[0-9x*]{1,3}(?:\.[0-9x*]{1,3}){3}$")
_CLUSTER_DNS_SUFFIXES = (".svc", ".svc.cluster.local")
# Names that never identify a cross-service dependency.
_PLATFORM_SERVICE_NAMES = {"kubernetes", "localhost"}
_ENV_NAME = r"([A-Za-z_][A-Za-z0-9_]*)"
_QUOTED_ENV_NAME = rf"""['"`]{_ENV_NAME}['"`]"""
# Environment variable reads across the languages this scanner reads.
_ENV_VAR_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        # Python: os.getenv("X"), getenv("X"); PHP: getenv('X')
        rf"(?<![\w$.])(?:os\.)?getenv\(\s*{_QUOTED_ENV_NAME}",
        # Python: os.environ["X"], os.environ.get("X"), environ["X"]
        rf"(?<![\w$.])(?:os\.)?environ(?:\.get\(|\[)\s*{_QUOTED_ENV_NAME}",
        # JavaScript/TypeScript: process.env.X, process.env["X"], import.meta.env.X
        rf"\b(?:process|import\.meta)\.env\.{_ENV_NAME}",
        rf"\b(?:process|import\.meta)\.env\[\s*{_QUOTED_ENV_NAME}\s*\]",
        # Go: os.Getenv("X"), os.LookupEnv("X")
        rf"\bos\.(?:Getenv|LookupEnv)\(\s*{_QUOTED_ENV_NAME}",
        # Java: System.getenv("X"); C#: Environment.GetEnvironmentVariable("X")
        rf"\b(?:System\.getenv|Environment\.GetEnvironmentVariable)\(\s*{_QUOTED_ENV_NAME}",
        # Ruby: ENV["X"], ENV.fetch("X"); PHP: $_ENV["X"]
        rf"(?<![\w.])(?:\$_)?ENV(?:\.fetch\(|\[)\s*{_QUOTED_ENV_NAME}",
    )
)


class ReferenceKind(str, Enum):
    """How a service refers to one of its dependencies.

    URL: a literal http(s) URL; ``endpoint`` is that URL.
    ENV_VAR: an endpoint-like environment variable read in code;
        ``endpoint`` is the variable name, to be resolved from configuration.
    """

    URL = "URL"
    ENV_VAR = "ENV_VAR"


class EndpointReference(BaseModel):
    """A dependency reference found in a file owned by a specific microservice."""

    service_name: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    kind: ReferenceKind = ReferenceKind.URL

    def dependency_target(self) -> str | None:
        """Return the edge target this reference names.

        A URL is reduced to its hostname; an environment variable becomes a
        per-service ``env:`` reference, resolved later through the
        variable's Kubernetes-configured value when one is known.
        """
        if self.kind is ReferenceKind.ENV_VAR:
            return env_var_reference(self.service_name, self.endpoint)
        return urlsplit(self.endpoint).hostname or None


class BoundarySource(str, Enum):
    """Where a service boundary came from.

    MANIFEST: declared in the repository's ``release-coord.yaml``.
    MARKER: inferred from a Dockerfile, package.json or go.mod.
    """

    MANIFEST = "MANIFEST"
    MARKER = "MARKER"


class ServiceBoundary(BaseModel):
    """A service and the repository directory it owns.

    ``root`` is repository-relative without leading or trailing slashes;
    the empty string is the repository root. ``namespace`` is the
    Kubernetes namespace the service runs in when the manifest declares it,
    otherwise None (inferred during reconciliation).
    """

    name: str = Field(pattern=_DNS_LABEL_PATTERN.pattern)
    root: str
    source: BoundarySource
    namespace: str | None = Field(default=None, pattern=_DNS_LABEL_PATTERN.pattern)


class ServiceScan(BaseModel):
    """One service found in a repository and the dependencies it references."""

    service: ServiceBoundary
    references: list[EndpointReference] = Field(default_factory=list)


class RepositoryScan(BaseModel):
    """Every service found in one repository, read at one pinned commit."""

    revision: ScannedRevision
    services: list[ServiceScan] = Field(default_factory=list)


class CommitComparison(BaseModel):
    """How two commits of one repository relate, as the server reports it.

    ``merge_bases`` are the best common ancestors of ``base`` and
    ``target``; ``base`` is an ancestor of ``target`` exactly when it is
    one of them. ``changed_paths`` are the repository-relative paths (no
    leading slash) changed from the merge base to ``target``: what a pull
    request from ``base`` to ``target`` changes.
    """

    repository: str = Field(min_length=1)
    base: str = Field(min_length=1)
    target: str = Field(min_length=1)
    merge_bases: list[str] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)

    @property
    def base_is_ancestor(self) -> bool:
        """Whether ``base`` is an ancestor of (or equal to) ``target``."""
        return self.base == self.target or self.base in self.merge_bases


# Page size for commit diffs; the server's maximum.
_DIFF_PAGE_SIZE: Final[int] = 2000
_HEADS_PREFIX: Final[str] = "refs/heads/"


def _commit_version(commit_sha: str) -> GitVersionDescriptor:
    """Return the descriptor that pins a read to ``commit_sha``."""
    return GitVersionDescriptor(version=commit_sha, version_type="commit")


def _field(value: Any, snake: str, camel: str) -> Any:
    """Read a field from an SDK model or the raw JSON dict some endpoints
    return (commit diff changes arrive as camelCase dicts)."""
    if isinstance(value, dict):
        return value.get(camel, value.get(snake))
    return getattr(value, snake, None)


def _diff_path(change: Any) -> str | None:
    """Return a commit diff change's file path, relative and without a
    leading slash, or None for folders and entries without a path."""
    item = _field(change, "item", "item")
    if item is None or _field(item, "is_folder", "isFolder"):
        return None
    path = str(_field(item, "path", "path") or "").strip("/")
    return path or None


def resolve_default_branch_head(
    git_client: Any, repository: Any, project: str, repo_name: str
) -> ScannedRevision:
    """Return the commit the repository's default branch points at.

    Raises:
        ValueError: if the repository has no default branch, or the branch
            has no commit (for example an empty repository).
    """
    ref_name = getattr(repository, "default_branch", None) or ""
    if not ref_name.startswith(_HEADS_PREFIX):
        raise ValueError(f"{repo_name}: repository has no default branch")
    refs = git_client.get_refs(
        repository.id, project=project, filter=ref_name[len("refs/") :]
    )
    head = next((ref for ref in refs or [] if ref.name == ref_name), None)
    commit_sha = (getattr(head, "object_id", None) or "").lower()
    if not commit_sha:
        raise ValueError(f"{repo_name}: default branch {ref_name} has no commit")
    return ScannedRevision(
        repository=repo_name,
        branch=ref_name[len(_HEADS_PREFIX) :],
        commit_sha=commit_sha,
    )


class ServiceDeclaration(BaseModel):
    """One ``services`` entry of a ``release-coord.yaml`` manifest.

    ``namespace`` (optional) pins the Kubernetes namespace the service is
    deployed to. Without it the namespace is inferred, and a service name
    found in several scanned namespaces is ambiguous.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_DNS_LABEL_PATTERN.pattern)
    root: str = Field(default=".", validate_default=True)
    namespace: str | None = Field(default=None, pattern=_DNS_LABEL_PATTERN.pattern)

    @field_validator("root")
    @classmethod
    def _normalize_root(cls, root: str) -> str:
        segments = [s for s in root.strip().split("/") if s and s != "."]
        if ".." in segments:
            raise ValueError("root must stay inside the repository")
        return "/".join(segments)


class ReleaseCoordManifest(BaseModel):
    """Service boundaries declared in a repository's ``release-coord.yaml``.

    Example::

        services:
          - name: payments
            root: services/payments-api
            namespace: shop
          - name: gateway
            root: .
    """

    model_config = ConfigDict(extra="forbid")

    services: list[ServiceDeclaration] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_services(self) -> Self:
        names = [service.name for service in self.services]
        roots = [service.root for service in self.services]
        if len(set(names)) != len(names):
            raise ValueError("service names must be unique")
        if len(set(roots)) != len(roots):
            raise ValueError("service roots must be unique")
        return self


def parse_release_coord_manifest(content: str) -> ReleaseCoordManifest:
    """Parse and validate the text of a ``release-coord.yaml`` manifest.

    Raises:
        ValueError: if the text is not YAML or does not match the schema
            (pydantic's ValidationError is a ValueError).
    """
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ValueError(f"not valid YAML: {error}") from error
    return ReleaseCoordManifest.model_validate(document)


def _path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _is_scannable_path(path: str) -> bool:
    """Return True for application or configuration source files.

    Documentation, dependency/vendor trees, build output, and lock files are
    skipped because they mostly reference third-party sites.
    """
    segments = [segment.lower() for segment in _path_segments(path)]
    if not segments or _EXCLUDED_DIRECTORIES.intersection(segments[:-1]):
        return False
    filename = segments[-1]
    if filename in _EXCLUDED_FILENAMES:
        return False
    if filename == ".env" or filename.startswith(".env."):
        return True
    return os.path.splitext(filename)[1] in _TEXT_FILE_SUFFIXES


def _dns_label(name: str) -> str | None:
    """Normalize a directory or repository name to a DNS label, or None."""
    label = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    label = re.sub(r"-{2,}", "-", label).strip("-")[:63].rstrip("-")
    return label if _DNS_LABEL_PATTERN.match(label) else None


def _boundaries_from_manifest(manifest: ReleaseCoordManifest) -> list[ServiceBoundary]:
    return [
        ServiceBoundary(
            name=service.name,
            root=service.root,
            source=BoundarySource.MANIFEST,
            namespace=service.namespace,
        )
        for service in manifest.services
    ]


def _marker_boundary_root(path: str) -> list[str] | None:
    """Return the directory a marker file makes a service boundary, or None.

    Boundaries are the repository root, its immediate subdirectories, and
    the immediate children of conventional grouping directories
    (``src/``, ``services/``, ``apps/``, ``cmd/``).
    """
    segments = _path_segments(path)
    if not segments or segments[-1].lower() not in _SERVICE_MARKER_FILENAMES:
        return None
    directory = segments[:-1]
    if _EXCLUDED_DIRECTORIES.intersection(s.lower() for s in directory):
        return None
    if len(directory) <= 1:
        return directory
    if len(directory) == 2 and directory[0].lower() in _SERVICE_CONTAINER_DIRECTORIES:
        return directory
    return None


def _infer_service_boundaries(
    file_paths: list[str], repo_name: str
) -> list[ServiceBoundary]:
    """Infer service boundaries from marker files, in discovery order.

    A marker at the repository root names the service after the repository;
    any other boundary is named after its directory. Directories whose name
    cannot become a DNS label are skipped.
    """
    boundaries: dict[str, ServiceBoundary] = {}
    for path in file_paths:
        directory = _marker_boundary_root(path)
        if directory is None:
            continue
        root = "/".join(directory)
        if root in boundaries:
            continue
        name = _dns_label(directory[-1] if directory else repo_name)
        if name is None:
            logger.warning("Skipping service directory %r: not a DNS label", root)
            continue
        boundaries[root] = ServiceBoundary(
            name=name, root=root, source=BoundarySource.MARKER
        )
    return list(boundaries.values())


def _owning_boundary(
    path: str, boundaries: list[ServiceBoundary]
) -> ServiceBoundary | None:
    """Return the boundary with the deepest root containing ``path``."""
    segments = _path_segments(path)
    owner: ServiceBoundary | None = None
    owner_depth = -1
    for boundary in boundaries:
        root = _path_segments(boundary.root)
        if len(root) > owner_depth and segments[: len(root)] == root:
            owner, owner_depth = boundary, len(root)
    return owner


def _is_dependency_host(hostname: str) -> bool:
    """Return True for hosts that can be another service or infrastructure.

    Kept: in-cluster names (a single DNS label or a ``.svc`` cluster name),
    private DNS names (``*.internal.example.com``, ``*.corp``, private link
    zones) and IP addresses, so reconciliation can register whatever does
    not map to the inventory as an ``UnresolvedEntity``. Rejected: loopback
    and unspecified addresses, redacted placeholders such as ``x.x.x.x``,
    platform names like ``localhost`` and ``kubernetes``, and public domains.
    """
    host = hostname.lower().rstrip(".")
    if not host or _IP_PLACEHOLDER_PATTERN.match(host):
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return not (address.is_loopback or address.is_unspecified)
    service_label = host.split(".", 1)[0]
    if service_label in _PLATFORM_SERVICE_NAMES:
        return False
    if "." not in host:
        return bool(_DNS_LABEL_PATTERN.match(host))
    if host.endswith(_CLUSTER_DNS_SUFFIXES):
        return bool(_DNS_LABEL_PATTERN.match(service_label))
    return is_private_dns_host(host)


def _is_noise_endpoint(endpoint: str) -> bool:
    """Return True for URLs that are not a dependency of the service.

    Templated URLs, non-http(s) schemes, and URLs whose host fails
    ``_is_dependency_host`` are noise.
    """
    if "${" in endpoint or "{{" in endpoint:
        return True
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname or ""
    except ValueError:
        return True
    if parsed.scheme.lower() not in {"http", "https"}:
        return True
    return not _is_dependency_host(hostname)


def _extract_env_var_names(content: str) -> list[str]:
    """Return endpoint-like environment variables read in ``content``, in order."""
    found: list[tuple[int, str]] = []
    for pattern in _ENV_VAR_PATTERNS:
        found.extend((match.start(), match.group(1)) for match in pattern.finditer(content))
    names = [name for _, name in sorted(found)]
    return [name for name in dict.fromkeys(names) if is_endpoint_variable(name)]


def _extract_references(
    content: str, service_name: str, file_path: str
) -> list[EndpointReference]:
    """Return the dependency references in one file, URLs first."""
    references = [
        EndpointReference(
            service_name=service_name, endpoint=endpoint, file_path=file_path
        )
        for endpoint in (match.rstrip(".,;:") for match in _URL_PATTERN.findall(content))
        if not _is_noise_endpoint(endpoint)
    ]
    references.extend(
        EndpointReference(
            service_name=service_name,
            endpoint=name,
            file_path=file_path,
            kind=ReferenceKind.ENV_VAR,
        )
        for name in _extract_env_var_names(content)
    )
    return references


class DevOpsExtractor:
    """Read-only client boundary for DevOps pipeline and repository data."""

    def __init__(self, org_url: str, project: str, repositories: list[str]) -> None:
        """Bind the extractor to an explicit Azure DevOps scope.

        Args:
            org_url: Azure DevOps organization URL, e.g. https://dev.azure.com/org.
            project: Azure DevOps project name.
            repositories: Repository names in scope for this extractor.

        Raises:
            ValueError: If org_url or project is blank.
        """
        if not org_url or not org_url.strip():
            raise ValueError("org_url is required")
        if not project or not project.strip():
            raise ValueError("project is required")
        self._credential = DefaultAzureCredential()
        self._organization_url = org_url.strip()
        self._project = project.strip()
        self._repositories = list(repositories)

    def _get_connection(self) -> Connection:
        access_token = self._credential.get_token(_AZURE_DEVOPS_SCOPE)
        credentials = BasicTokenAuthentication({"access_token": access_token.token})
        return Connection(base_url=self._organization_url, creds=credentials)

    def _get_clients(self) -> tuple[Any, Any, str]:
        connection = self._get_connection()
        return (
            connection.clients.get_build_client(),
            connection.clients.get_git_client(),
            self._project,
        )

    def verify_access(self) -> tuple[bool, str]:
        """Confirm read access by fetching the configured project's metadata.

        Token acquisition and the project lookup share a strict
        VERIFY_TIMEOUT_SECONDS deadline so a hanging credential chain or
        network call cannot block the caller.

        Returns:
            (True, "Success") when the project is readable, otherwise
            (False, <error message>).
        """

        def _check() -> None:
            connection = self._get_connection()
            connection.clients.get_core_client().get_project(self._project)

        try:
            run_with_timeout(_check, VERIFY_TIMEOUT_SECONDS)
            return True, "Success"
        except TimeoutError:
            logger.warning("Azure DevOps access verification timed out")
            return False, TIMEOUT_MESSAGE
        except Exception as error:
            logger.warning("Azure DevOps access verification failed: %s", error)
            return False, str(error)

    @staticmethod
    def _read_item_content(
        git_client: Any,
        repository_id: str,
        path: str,
        project: str,
        commit_sha: str | None = None,
    ) -> str:
        """Read one file, at ``commit_sha`` when given (else the default branch)."""
        if commit_sha is None:
            content = git_client.get_item_content(repository_id, path, project=project)
        else:
            content = git_client.get_item_content(
                repository_id,
                path,
                project=project,
                version_descriptor=_commit_version(commit_sha),
            )
        if isinstance(content, (bytes, bytearray)):
            return bytes(content).decode("utf-8", errors="replace")
        return b"".join(content).decode("utf-8", errors="replace")

    @staticmethod
    def _parse_yaml_variables(content: str) -> dict[str, str]:
        variables: dict[str, str] = {}
        lines = content.splitlines()
        section_start = next(
            (
                index
                for index, line in enumerate(lines)
                if re.match(r"^variables\s*:", line)
            ),
            None,
        )
        if section_start is None:
            return variables

        pending_name: str | None = None
        for line in lines[section_start + 1 :]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            if indent == 0:
                break

            list_entry = re.match(r"^\s*-\s*name\s*:\s*(.+?)\s*$", line)
            if list_entry:
                pending_name = list_entry.group(1).strip("\"'")
                continue

            value_entry = re.match(r"^\s+value\s*:\s*(.*?)\s*$", line)
            if pending_name and value_entry:
                value = value_entry.group(1).strip()
                variables[pending_name] = value.strip("\"'")
                pending_name = None
                continue

            mapping_entry = re.match(
                r"^\s*(?:-\s*)?([A-Za-z_][\w.-]*)\s*:\s*(.*?)\s*$", line
            )
            if mapping_entry and mapping_entry.group(1) not in {
                "name",
                "value",
                "group",
            }:
                key, value = mapping_entry.groups()
                variables[key] = value.strip().strip("\"'")
                pending_name = None

        return variables

    @staticmethod
    def _find_repository(git_client: Any, project: str, repo_name: str) -> Any:
        return next(
            (
                repository
                for repository in git_client.get_repositories(project)
                if repository.name == repo_name
            ),
            None,
        )

    def get_pipeline_variables(self, repo_name: str) -> dict[str, str]:
        """Return the configured variables for a repository's pipeline.

        This method is intended to read pipeline configuration only. It must
        not create, update, or delete pipelines, variables, or repository data.

        Args:
            repo_name: Name of the repository whose pipeline variables to read.

        Returns:
            A mapping of pipeline variable names to their string values.
        """
        try:
            build_client, git_client, project = self._get_clients()
            source_repository = self._find_repository(
                git_client, project, repo_name
            )
            if source_repository is None:
                logger.warning(
                    "Azure DevOps repository %s was not found", repo_name
                )
                return {}

            definitions = build_client.get_definitions(
                project=project,
                repository_id=source_repository.id,
                repository_type=getattr(source_repository, "type", "TfsGit"),
            )
            if not definitions:
                logger.warning(
                    "No Azure DevOps pipeline definitions found for repository %s",
                    repo_name,
                )
                return {}

            variables: dict[str, str] = {}
            for definition_summary in definitions:
                definition = build_client.get_definition(
                    definition_id=definition_summary.id, project=project
                )
                process = getattr(definition, "process", None)
                yaml_filename = getattr(process, "yaml_filename", None)
                if yaml_filename:
                    definition_repository = getattr(
                        definition, "repository", None
                    )
                    repository_id = (
                        getattr(definition_repository, "id", None)
                        or source_repository.id
                    )
                    if repository_id:
                        yaml_content = self._read_item_content(
                            git_client, repository_id, yaml_filename, project
                        )
                        variables.update(self._parse_yaml_variables(yaml_content))

                definition_variables = getattr(definition, "variables", None) or {}
                for name, variable in definition_variables.items():
                    value = getattr(variable, "value", None)
                    if value is not None and not getattr(variable, "is_secret", False):
                        variables[name] = str(value)
            return variables
        except Exception:
            logger.exception(
                "Failed to read pipeline variables for repository %s", repo_name
            )
            return {}

    def compare_commits(
        self, repo_name: str, base: str, target: str
    ) -> DiscoveryResult[CommitComparison]:
        """Report how ``base`` and ``target`` relate and what ``target`` changes.

        Read-only. Both commits must exist in the repository. Every page of
        the diff is read, so ``changed_paths`` is complete.

        Returns:
            SUCCESS with one ``CommitComparison``, or FAILED when the
            repository or a commit can't be found or the server can't be
            read.
        """
        base, target = base.strip().lower(), target.strip().lower()
        try:
            _, git_client, project = self._get_clients()
            repository = self._find_repository(git_client, project, repo_name)
            if repository is None:
                return DiscoveryResult[CommitComparison].failed(
                    f"{repo_name}: repository was not found"
                )
            # Resolve to the full commit IDs the server knows, so an
            # abbreviated SHA compares equal to the merge bases it returns.
            base, target = (
                (getattr(git_client.get_commit(sha, repository.id, project=project),
                         "commit_id", None) or sha).lower()
                for sha in (base, target)
            )
            merge_bases = [
                (getattr(ref, "commit_id", None) or "").lower()
                for ref in git_client.get_merge_bases(
                    repository.id, base, target, project=project
                )
                or []
            ]
            changed: list[str] = []
            skip = 0
            while True:
                diffs = git_client.get_commit_diffs(
                    repository.id,
                    project=project,
                    diff_common_commit=True,
                    top=_DIFF_PAGE_SIZE,
                    skip=skip,
                    base_version_descriptor=GitBaseVersionDescriptor(
                        base_version=base, base_version_type="commit"
                    ),
                    target_version_descriptor=GitTargetVersionDescriptor(
                        target_version=target, target_version_type="commit"
                    ),
                )
                changes = list(getattr(diffs, "changes", None) or [])
                changed.extend(path for path in map(_diff_path, changes) if path)
                if getattr(diffs, "all_changes_included", True) or not changes:
                    break
                skip += len(changes)
            return DiscoveryResult[CommitComparison].success(
                [
                    CommitComparison(
                        repository=repo_name,
                        base=base,
                        target=target,
                        merge_bases=[sha for sha in merge_bases if sha],
                        changed_paths=list(dict.fromkeys(changed)),
                    )
                ]
            )
        except Exception as error:
            logger.warning("Cannot compare %s %s..%s: %s", repo_name, base, target, error)
            return DiscoveryResult[CommitComparison].failed(
                f"{repo_name}: cannot compare {base[:12]}..{target[:12]}: {error}"
            )

    def _repository_boundaries(
        self,
        git_client: Any,
        repository: Any,
        project: str,
        repo_name: str,
        file_paths: list[str],
        commit_sha: str,
    ) -> list[ServiceBoundary]:
        """Return the repository's service boundaries.

        ``release-coord.yaml`` at the repository root, when present, is
        authoritative; otherwise boundaries are inferred from marker files.

        Raises:
            ValueError: if the manifest exists but is invalid.
        """
        if RELEASE_COORD_MANIFEST_PATH in file_paths:
            content = self._read_item_content(
                git_client, repository.id, RELEASE_COORD_MANIFEST_PATH, project, commit_sha
            )
            return _boundaries_from_manifest(parse_release_coord_manifest(content))
        return _infer_service_boundaries(file_paths, repo_name)

    def scan_repository(
        self,
        repo_name: str,
        commit: ScannedRevision | None = None,
        only_roots: set[str] | None = None,
    ) -> DiscoveryResult[RepositoryScan]:
        """Find the services in a repository and the dependencies they reference.

        The default branch is first resolved to one commit (or ``commit`` is
        read, such as a pull request's head), and every file is listed and
        read at that commit, so the recorded revision is exactly what was
        scanned even if the branch moves meanwhile. With ``only_roots``,
        only the services whose root is listed are returned and read.

        Service boundaries come from ``release-coord.yaml`` when the
        repository has one, otherwise from directories (the root and its
        immediate subdirectories) holding a Dockerfile, package.json or
        go.mod. Each scannable file is attributed to the boundary with the
        deepest enclosing root; files outside every boundary are not read.
        References are internal, private-DNS and IP URLs plus endpoint-like
        environment variables (see ``_extract_references``).

        Every service boundary is returned, even with no references: a
        service that calls nothing is still part of the inventory and can
        still be called. It must not write to the repository or trigger
        builds or deployments.

        Args:
            repo_name: Name of the repository to inspect.

        Returns:
            SUCCESS with one ``RepositoryScan`` holding the scanned revision
            and one ``ServiceScan`` per service, each with its unique
            references in discovery order, or FAILED if the repository could
            not be found or scanned, has no default branch commit, or its
            manifest is invalid.
        """
        try:
            _, git_client, project = self._get_clients()
            repository = self._find_repository(git_client, project, repo_name)
            if repository is None:
                logger.warning("Azure DevOps repository %s was not found", repo_name)
                return DiscoveryResult[RepositoryScan].failed(
                    f"{repo_name}: repository was not found"
                )
            if commit is None:
                try:
                    revision = resolve_default_branch_head(
                        git_client, repository, project, repo_name
                    )
                except ValueError as error:
                    logger.warning("Cannot pin %s to a commit: %s", repo_name, error)
                    return DiscoveryResult[RepositoryScan].failed(str(error))
            else:
                revision = commit
            commit_sha = revision.commit_sha

            items = git_client.get_items(
                repository.id,
                project=project,
                recursion_level="Full",
                version_descriptor=_commit_version(commit_sha),
            )
            file_paths = [
                getattr(item, "path", "")
                for item in items
                if not getattr(item, "is_folder", False)
            ]
            try:
                boundaries = self._repository_boundaries(
                    git_client, repository, project, repo_name, file_paths, commit_sha
                )
            except ValueError as error:
                logger.warning("Invalid release-coord.yaml in %s: %s", repo_name, error)
                return DiscoveryResult[RepositoryScan].failed(
                    f"{repo_name}: invalid release-coord.yaml: {error}"
                )
            if only_roots is not None:
                boundaries = [b for b in boundaries if b.root in only_roots]

            references: dict[str, dict[tuple[ReferenceKind, str], EndpointReference]] = {
                boundary.root: {} for boundary in boundaries
            }
            for path in file_paths:
                if path == RELEASE_COORD_MANIFEST_PATH or not _is_scannable_path(path):
                    continue
                owner = _owning_boundary(path, boundaries)
                if owner is None:
                    continue
                content = self._read_item_content(
                    git_client, repository.id, path, project, commit_sha
                )
                for reference in _extract_references(content, owner.name, path):
                    references[owner.root].setdefault(
                        (reference.kind, reference.endpoint), reference
                    )

            scans = [
                ServiceScan(
                    service=boundary,
                    references=list(references[boundary.root].values()),
                )
                for boundary in boundaries
            ]
            return DiscoveryResult[RepositoryScan].success(
                [RepositoryScan(revision=revision, services=scans)]
            )
        except Exception as error:
            logger.exception("Failed to scan repository %s", repo_name)
            return DiscoveryResult[RepositoryScan].failed(
                f"{repo_name}: repository scan failed: {error}"
            )

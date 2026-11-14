"""Read-only clients for extracting Azure DevOps repository and pipeline data."""

import ipaddress
import logging
import os
import re
from collections.abc import Collection
from enum import Enum
from typing import Any, Final, Self

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

from src.core.models import (
    ConfigBinding,
    ConfigFramework,
    DiscoveryResult,
    OpaqueReferenceKind,
    OpaqueReferenceSite,
    PipelineVariables,
    RepositoryCoverage,
    ScannedRevision,
    ServiceCoverage,
    ServiceStaticConfig,
    SharedLibrary,
    UnscannedFile,
    UnscannedReason,
    storable_pipeline_value,
)
from src.core.env_names import (
    endpoint_in_value,
    endpoint_of_value,
    is_endpoint_variable,
    is_loopback_endpoint,
    names_host,
)
from src.core.service_ids import env_var_reference, host_of, is_private_dns_host
from src.tools.build_projects import (
    BuildProject,
    ProjectKind,
    RepositoryLayout,
    build_file_system,
    build_layout,
    dotnet_project_roots,
    is_gradle_settings,
    read_projects,
    unresolved_sites,
)
from src.tools.client_libraries import client_libraries, manifest_ecosystem
from src.tools.proxy_configs import ProxyRead, is_envoy_config, proxy_upstreams, template_suffix
from src.tools.config_readers import (
    ConfigFileKind,
    ConfigParseError,
    ImageEnvironment,
    config_file,
    dockerfile_environment,
    dotnet_code_bindings,
    dotnet_settings_bindings,
    env_binding,
    env_default_bindings,
    feign_client_services,
    fill_placeholder_defaults,
    flatten_dotnet_settings,
    merge_bindings,
    spring_code_bindings,
    spring_config_bindings,
    spring_documents,
    storable_image_environment,
)
from src.tools.reference_sites import (
    BoundSection,
    bound_sections,
    external_config_sites,
    followed_env_reads,
    opaque_reference_sites,
    unbound_section_sites,
)
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
    # JavaScript-family files the JavaScript patterns already read.
    ".cjs",
    ".jsx",
    ".mjs",
    ".svelte",
    ".tsx",
    ".vue",
}
# Source code the scanner can't read: a dependency declared in one of these
# files is invisible, so the file makes its service's coverage incomplete.
_UNSUPPORTED_SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".c", ".cc", ".clj", ".cpp", ".cxx", ".dart", ".erl", ".ex", ".exs",
        ".fs", ".fsx", ".groovy", ".h", ".hpp", ".hs", ".jl", ".kt", ".kts",
        ".lua", ".m", ".ml", ".nim", ".pl", ".pm", ".r", ".rs", ".scala",
        ".swift", ".vb", ".zig",
    }
)
# Literal URLs with a scheme that addresses another service (see
# ENDPOINT_SCHEMES); ``_is_noise_endpoint`` drops the other schemes.
_URL_PATTERN = re.compile(
    r"""(?:jdbc:)?[a-z][a-z0-9+.-]*://[^\s"'<>)}\]`]+""", re.IGNORECASE
)
_EXCLUDED_DIRECTORIES = {
    ".git",
    ".gradle",
    "bower_components",
    "build",
    "dist",
    "docs",
    "node_modules",
    "obj",
    "target",
    "vendor",
}
# Test code (it never runs in production, so its addresses are not runtime
# dependencies): conventional test directories, ``*-tests``/``*.UnitTests``
# style directories, and test file names (Go, JS/TS, Python).
_TEST_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"__tests__", "e2e", "fixtures", "test", "test-data", "test_data", "testdata", "testing", "tests"}
)
_TEST_DIRECTORY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^.+[-_.](?:unit|integration|functional|acceptance|e2e|load|perf|smoke)?[-_]?tests?$"
    r"|^.+[-_.]e2e$",
    re.IGNORECASE,
)
_TEST_FILE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:_test\.go|\.(?:test|spec)\.[cm]?[jt]sx?|_test\.py)$|^test_[^/]*\.py$", re.IGNORECASE
)
# Directories that hold tooling, never a deployable service, even with a
# marker file (a Dockerfile for a local Prometheus, CI images...).
_TOOLING_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {".devcontainer", ".github", "docker", "hack", "scripts"}
)
# Directories holding deployment configuration (charts, manifests), whose
# copies of a proxy configuration are not a service of their own.
_DEPLOYMENT_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"charts", "deploy", "deployment", "deployments", "helm", "k8s", "kubernetes", "manifests"}
)
# Build output only inside a .NET project directory (Express keeps real
# source in bin/www).
_DOTNET_OUTPUT_DIRECTORY: Final[str] = "bin"
# Build definitions read for project structure (see build_projects), not
# for references; the Kotlin DSL ones would otherwise be unsupported source.
_BUILD_DEFINITION_FILENAMES: Final[frozenset[str]] = frozenset(
    {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}
)
# Local development orchestration (Docker Compose): describes a whole stack
# on one machine, so its URLs belong to other services, not the owner.
_COMPOSE_FILE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:docker-)?compose(?:[.-][^/]*)?\.ya?ml$", re.IGNORECASE
)
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

    URL: a literal URL with an endpoint scheme (http, gRPC, AMQP, JDBC...);
        ``endpoint`` is that URL.
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
        endpoint = endpoint_of_value(self.endpoint)
        return (host_of(endpoint) or None) if endpoint else None


class BoundarySource(str, Enum):
    """Where a service boundary came from.

    MANIFEST: declared in the repository's ``release-coord.yaml``.
    MARKER: inferred from a Dockerfile, package.json or go.mod at a shallow
        depth, or a runnable (or containerized) build project at any depth
        (see ``build_projects``).
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
    coverage: ServiceCoverage | None = None
    config: ServiceStaticConfig | None = None


class RepositoryScan(BaseModel):
    """Every service found in one repository, read at one pinned commit."""

    revision: ScannedRevision
    services: list[ServiceScan] = Field(default_factory=list)
    unowned_source_files: list[str] = Field(default_factory=list)
    libraries: list[SharedLibrary] = Field(default_factory=list)

    def coverage(self) -> RepositoryCoverage:
        """Return the scan's coverage record, as a snapshot stores it."""
        return RepositoryCoverage(
            repository=self.revision.repository,
            commit_sha=self.revision.commit_sha,
            services=[scan.coverage for scan in self.services if scan.coverage],
            unowned_source_files=self.unowned_source_files,
            libraries=self.libraries,
        )

    def static_config(self) -> list[ServiceStaticConfig]:
        """Return each service's static configuration, as a snapshot stores it."""
        return [scan.config for scan in self.services if scan.config]


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


class FileClass(str, Enum):
    """How the scanner treats one repository file.

    SCANNED: application or configuration source the scanner reads
        (a Dockerfile for its image ``ENV``).
    EXCLUDED: skipped by design (documentation, vendored dependencies,
        build output, lock files), which mostly reference third-party sites,
        Docker Compose files, which describe a local stack of other
        services, and test code (see ``is_test_directory``), which never
        runs in production.
    UNSUPPORTED: source code in a language the scanner can't read.
    NOT_SOURCE: anything else (images, Markdown, licenses...).
    """

    SCANNED = "SCANNED"
    EXCLUDED = "EXCLUDED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_SOURCE = "NOT_SOURCE"


def is_test_directory(name: str) -> bool:
    """Whether a directory name marks test code (``tests``, ``Ordering.UnitTests``)."""
    return name.lower() in _TEST_DIRECTORY_NAMES or bool(_TEST_DIRECTORY_PATTERN.match(name))


def _in_excluded_directory(directories: list[str], dotnet_roots: Collection[str]) -> bool:
    """Whether a file under ``directories`` (original case) is excluded by
    directory: an always-excluded name, or ``bin/`` of a .NET project."""
    for position, directory in enumerate(directories):
        name = directory.lower()
        if name in _EXCLUDED_DIRECTORIES or is_test_directory(name):
            return True
        if name == _DOTNET_OUTPUT_DIRECTORY and "/".join(directories[:position]) in dotnet_roots:
            return True
    return False


def classify_path(path: str, dotnet_roots: Collection[str] = frozenset()) -> FileClass:
    """Classify a repository file for scanning and coverage accounting.

    ``dotnet_roots`` are the directories holding a ``*.csproj``, whose
    ``bin/`` is build output.
    """
    original = _path_segments(path)
    segments = [segment.lower() for segment in original]
    if not segments:
        return FileClass.NOT_SOURCE
    filename = segments[-1]
    if (
        _in_excluded_directory(original[:-1], dotnet_roots)
        or filename in _EXCLUDED_FILENAMES
        or _COMPOSE_FILE_PATTERN.match(filename)
        or _TEST_FILE_PATTERN.search(filename)
    ):
        return FileClass.EXCLUDED
    if filename in _BUILD_DEFINITION_FILENAMES:
        return FileClass.NOT_SOURCE
    if filename == ".env" or filename.startswith(".env.") or filename == "dockerfile":
        return FileClass.SCANNED
    suffix = template_suffix(filename) or os.path.splitext(filename)[1]
    if suffix in _TEXT_FILE_SUFFIXES:
        return FileClass.SCANNED
    if suffix in _UNSUPPORTED_SOURCE_SUFFIXES:
        return FileClass.UNSUPPORTED
    return FileClass.NOT_SOURCE


def _is_scannable_path(path: str) -> bool:
    """Return True for application or configuration source files."""
    return classify_path(path) is FileClass.SCANNED


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


def _marker_boundary_root(
    path: str, dotnet_roots: Collection[str] = frozenset()
) -> list[str] | None:
    """Return the directory a marker file makes a service boundary, or None.

    Boundaries are the repository root, its immediate subdirectories, and
    the immediate children of conventional grouping directories
    (``src/``, ``services/``, ``apps/``, ``cmd/``).
    """
    segments = _path_segments(path)
    if not segments or segments[-1].lower() not in _SERVICE_MARKER_FILENAMES:
        return None
    directory = segments[:-1]
    if _in_excluded_directory(directory, dotnet_roots):
        return None
    if directory and directory[-1].lower() in _TOOLING_DIRECTORY_NAMES:
        return None
    if len(directory) <= 1:
        return directory
    if len(directory) == 2 and directory[0].lower() in _SERVICE_CONTAINER_DIRECTORIES:
        return directory
    return None


def _nested_boundary_root(path: str, dotnet_roots: Collection[str]) -> list[str] | None:
    """Return the directory a deeper marker makes a candidate boundary, or None.

    A Dockerfile anywhere under a grouping directory
    (``src/accounts/contacts/Dockerfile``), and an Envoy configuration
    anywhere outside deployment directories (a gateway run from the stock
    Envoy image has no code of its own). Candidates inside another
    boundary are dropped by ``_infer_service_boundaries``.
    """
    segments = _path_segments(path)
    if len(segments) < 2:
        return None
    directory = segments[:-1]
    lowered = [segment.lower() for segment in directory]
    if _in_excluded_directory(directory, dotnet_roots) or any(
        name in _TOOLING_DIRECTORY_NAMES or name in _DEPLOYMENT_DIRECTORY_NAMES for name in lowered
    ):
        return None
    filename = segments[-1].lower()
    if filename == "dockerfile" and lowered[0] in _SERVICE_CONTAINER_DIRECTORIES:
        return directory
    if is_envoy_config(filename):
        return directory
    return None


def _service_project_roots(
    file_paths: list[str], projects: list[BuildProject], dotnet_roots: Collection[str]
) -> dict[str, list[str]]:
    """Build-file path -> directory for projects that are services.

    A project is a service when it is runnable, or when its directory has
    a Dockerfile and it is a library (so it is packaged as an image). Test,
    AppHost and aggregator projects never are. Any depth counts.
    """
    with_dockerfile = {
        "/".join(_path_segments(path)[:-1])
        for path in file_paths
        if _path_segments(path) and _path_segments(path)[-1].lower() == "dockerfile"
    }
    found: dict[str, list[str]] = {}
    for project in projects:
        directory = _path_segments(project.root)
        if _in_excluded_directory(directory, dotnet_roots):
            continue
        if project.kind is ProjectKind.SERVICE or (
            project.kind is ProjectKind.LIBRARY and project.root in with_dockerfile
        ):
            found[project.file_path] = directory
    return found


def _infer_service_boundaries(
    file_paths: list[str],
    repo_name: str,
    projects: list[BuildProject] | None = None,
) -> list[ServiceBoundary]:
    """Infer service boundaries from marker files, in discovery order.

    Markers are a Dockerfile, package.json or go.mod at a shallow depth
    (see ``_marker_boundary_root``) and service build projects at any depth
    (see ``_service_project_roots``). Deeper Dockerfiles and Envoy
    configurations (see ``_nested_boundary_root``) are boundaries only
    when no other boundary contains them or lies inside them. A marker at the repository root names
    the service after the repository; any other boundary is named after its
    directory (a ``src`` directory after its parent, as ``cart/src``).
    Directories whose name cannot become a DNS label are skipped. A root
    whose only marker is a package.json is workspace tooling, not a
    service, when the repository has other boundaries.
    """
    dotnet_roots = dotnet_project_roots(file_paths)
    project_roots = _service_project_roots(file_paths, projects or [], dotnet_roots)
    boundaries: dict[str, ServiceBoundary] = {}
    root_markers: set[str] = set()
    nested: list[list[str]] = []
    for path in file_paths:
        directory = _marker_boundary_root(path, dotnet_roots)
        if directory == []:
            root_markers.add(_path_segments(path)[-1].lower())
        if directory is None:
            directory = project_roots.get(path.lstrip("/"))
        if directory is None:
            candidate = _nested_boundary_root(path, dotnet_roots)
            if candidate is not None:
                nested.append(candidate)
            continue
        root = "/".join(directory)
        if root in boundaries:
            continue
        name = _dns_label(_boundary_directory_name(directory) or repo_name)
        if name is None:
            logger.warning("Skipping service directory %r: not a DNS label", root)
            continue
        boundaries[root] = ServiceBoundary(
            name=name, root=root, source=BoundarySource.MARKER
        )
    if "" in boundaries and root_markers == {"package.json"} and len(boundaries) > 1:
        del boundaries[""]
    for directory in sorted(nested, key=len):
        root = "/".join(directory)
        others = [_path_segments(other) for other in boundaries if other]
        if root in boundaries or any(
            directory[: len(other)] == other or other[: len(directory)] == directory for other in others
        ):
            continue
        name = _dns_label(_boundary_directory_name(directory) or repo_name)
        if name is not None:
            boundaries[root] = ServiceBoundary(name=name, root=root, source=BoundarySource.MARKER)
    return list(boundaries.values())


def _boundary_directory_name(directory: list[str]) -> str | None:
    """The name a boundary directory gives its service (None at the root).

    A trailing ``src`` names nothing (``src/cart/src`` is the cart).
    """
    meaningful = list(directory)
    while len(meaningful) > 1 and meaningful[-1].lower() == "src":
        meaningful.pop()
    return meaningful[-1] if meaningful else None


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

    Templated URLs, schemes that don't address a service (see
    ``ENDPOINT_SCHEMES``), and URLs whose host fails ``_is_dependency_host``
    are noise.
    """
    if "${" in endpoint or "{{" in endpoint:
        return True
    normalized = endpoint_of_value(endpoint)
    if normalized is None:
        return True
    return not _is_dependency_host(host_of(normalized))


def _extract_env_var_names(content: str) -> list[str]:
    """Return endpoint-like environment variables read in ``content``, in order."""
    found: list[tuple[int, str]] = []
    for pattern in _ENV_VAR_PATTERNS:
        found.extend((match.start(), match.group(1)) for match in pattern.finditer(content))
    names = [name for _, name in sorted(found)]
    return [name for name in dict.fromkeys(names) if is_endpoint_variable(name)]


_COMMENT_LINE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(?://|#|/\*|\*|<!--).*$", re.MULTILINE)


def strip_comment_lines(content: str) -> str:
    """Blank whole-line comments (``//``, ``#``, ``/*``, `` *``, ``<!--``),
    keeping line numbers."""
    return _COMMENT_LINE.sub("", content)


def _extract_references(
    content: str, service_name: str, file_path: str
) -> list[EndpointReference]:
    """Return the dependency references in one file, URLs first.

    Whole-line comments are skipped (a commented-out address is not a
    dependency).
    """
    content = strip_comment_lines(content)
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


class FileRead(BaseModel):
    """What one scanned file contributes to its service.

    ``references`` are literal URLs and environment reads; ``bindings`` the
    configuration keys it reads or declares (see ``config_readers``);
    ``image`` the ``ENV`` of a Dockerfile. ``sites`` are references read
    but not understood and ``sections`` configuration sections bound as a
    whole (see ``reference_sites``); ``config_keys`` every key a framework
    configuration file declares, by framework.
    """

    references: list[EndpointReference] = Field(default_factory=list)
    bindings: list[ConfigBinding] = Field(default_factory=list)
    image: ImageEnvironment | None = None
    sites: list[OpaqueReferenceSite] = Field(default_factory=list)
    sections: list[BoundSection] = Field(default_factory=list)
    config_keys: dict[ConfigFramework, list[str]] = Field(default_factory=dict)


def read_source_file(content: str, service_name: str, path: str) -> FileRead:
    """Read one file a service owns.

    Framework configuration files (appsettings*.json, application*.yml)
    contribute bindings only: a literal URL in them is a default the
    runtime environment may override, never a fixed edge. A Dockerfile
    contributes its image ``ENV``. Code contributes literal URLs and
    environment reads (a URL that is only a read's default is that
    default, not a separate edge), C# IConfiguration and Spring reads,
    ``@FeignClient`` service names, environment reads through names the
    file fixes to literals (``followed_env_reads``) and, for an nginx or
    Envoy configuration, its upstreams (see ``proxy_configs``).

    Raises:
        ConfigParseError: if a framework configuration file can't be parsed.
    """
    relative = path.lstrip("/")
    kind = config_file(path)
    if kind is not None and kind.kind is ConfigFileKind.DOCKERFILE:
        return FileRead(image=dockerfile_environment(content))
    if kind is not None and kind.kind is ConfigFileKind.DOTNET_SETTINGS:
        return FileRead(
            bindings=dotnet_settings_bindings(content, relative, kind.profile),
            config_keys={ConfigFramework.DOTNET: list(flatten_dotnet_settings(content))},
        )
    if kind is not None and kind.kind is ConfigFileKind.SPRING_CONFIG:
        return FileRead(
            bindings=spring_config_bindings(content, relative, kind.profile),
            sites=external_config_sites(content),
            config_keys={
                ConfigFramework.SPRING: [
                    key
                    for _, properties in spring_documents(content, relative, kind.profile)
                    for key in properties
                ]
            },
        )
    bindings = env_default_bindings(content, relative)
    suffix = os.path.splitext(path)[1].lower()
    references = _extract_references(content, service_name, path)
    for read in followed_env_reads(content, path):
        binding = env_binding(read.name, read.default, relative)
        if binding is not None:
            bindings.append(binding)
            references.append(
                EndpointReference(
                    service_name=service_name, endpoint=read.name, file_path=path, kind=ReferenceKind.ENV_VAR
                )
            )
    if suffix == ".cs":
        bindings.extend(dotnet_code_bindings(content, relative))
    elif suffix == ".java":
        bindings.extend(spring_code_bindings(content, relative))
        references.extend(
            EndpointReference(service_name=service_name, endpoint=f"lb://{name}", file_path=path)
            for name in feign_client_services(content)
        )
    proxy = proxy_upstreams(content, path)
    if proxy is not None:
        references.extend(_proxy_references(proxy, service_name, path))
    code_defaults = {default.template for binding in bindings for default in binding.defaults}
    references = [
        reference
        for reference in references
        if reference.kind is not ReferenceKind.URL
        or endpoint_of_value(reference.endpoint) not in code_defaults
    ]
    return FileRead(
        references=_unique_references(references),
        bindings=bindings,
        sites=opaque_reference_sites(content, path)
        + (proxy.sites if proxy is not None else [])
        + (_unfollowed_proxy_variables(proxy, content) if proxy is not None else []),
        sections=bound_sections(content, path),
    )


def _proxy_references(proxy: ProxyRead, service_name: str, path: str) -> list[EndpointReference]:
    """References for a proxy configuration's upstreams (see ``proxy_configs``)."""
    references = [
        EndpointReference(service_name=service_name, endpoint=url, file_path=path)
        for url in proxy.urls
        if not _is_noise_endpoint(url)
    ]
    references.extend(
        EndpointReference(service_name=service_name, endpoint=name, file_path=path, kind=ReferenceKind.ENV_VAR)
        for name in proxy.variables
        if names_host(name)
    )
    return references


def _unfollowed_proxy_variables(proxy: ProxyRead, content: str) -> list[OpaqueReferenceSite]:
    """Upstream hosts from a variable whose name doesn't say it holds a host.

    Resolution only reads a bare host from host-named variables
    (``names_host``), so any other name can't be followed.
    """
    return [
        OpaqueReferenceSite(
            line=_line_of(content, f"${{{name}}}"), kind=OpaqueReferenceKind.CLIENT_ADDRESS_EXPRESSION
        )
        for name in proxy.variables
        if not names_host(name)
    ]


def _line_of(content: str, needle: str) -> int:
    position = content.find(needle)
    return content.count("\n", 0, position) + 1 if position >= 0 else 1


def _unique_references(references: list[EndpointReference]) -> list[EndpointReference]:
    seen: set[tuple[ReferenceKind, str]] = set()
    unique: list[EndpointReference] = []
    for reference in references:
        key = (reference.kind, reference.endpoint)
        if key not in seen:
            seen.add(key)
            unique.append(reference)
    return unique


def _default_is_dependency(template: str) -> bool:
    """Whether a default may address a dependency (not a public site).

    Defaults naming no address yet (a bare placeholder) and this host
    count: the first may be set at runtime, the second is reported.
    """
    endpoint = endpoint_in_value(fill_placeholder_defaults(template), connection_string=True)
    if endpoint is None or is_loopback_endpoint(endpoint):
        return True
    return _is_dependency_host(host_of(endpoint))


def dependency_bindings(bindings: list[ConfigBinding]) -> list[ConfigBinding]:
    """Merge a service's bindings, dropping those that only name public sites.

    As literal public URLs are noise to the scanner, a key whose every
    default is a public address (``"OpenAI:Endpoint": "https://..."``) is
    not a dependency reference.
    """
    kept = [
        binding
        for binding in bindings
        if not binding.defaults
        or any(_default_is_dependency(default.template) for default in binding.defaults)
    ]
    return merge_bindings(kept)


def merge_images(images: list[tuple[str, ImageEnvironment]]) -> tuple[str | None, ImageEnvironment]:
    """Combine the Dockerfiles a service owns into the ENV its image has.

    One Dockerfile is the image. With several, a name every one of them
    sets to the same literal keeps it; any other name one sets is opaque,
    since which file builds the deployed image is unknown.
    """
    if not images:
        return None, ImageEnvironment()
    if len(images) == 1:
        return images[0]
    names = {name for _, image in images for name in (*image.variables, *image.opaque)}
    merged = ImageEnvironment()
    opaque: set[str] = set()
    for name in sorted(names):
        values = {image.variables.get(name) for _, image in images}
        if len(values) == 1 and None not in values:
            merged.variables[name] = values.pop() or ""
        else:
            opaque.add(name)
    merged.opaque = sorted(opaque)
    return ", ".join(path for path, _ in images), merged


def build_static_config(
    repository: str,
    boundary: ServiceBoundary,
    commit_sha: str,
    bindings: list[ConfigBinding],
    images: list[tuple[str, ImageEnvironment]],
) -> ServiceStaticConfig:
    """Assemble one service's static configuration record."""
    image_file, image = merge_images(images)
    kept, withheld = storable_image_environment(image)
    return ServiceStaticConfig(
        repository=repository,
        root=boundary.root,
        service_name=boundary.name,
        commit_sha=commit_sha,
        bindings=dependency_bindings(bindings),
        image_variables=kept,
        image_withheld_variables=withheld,
        image_opaque_variables=image.opaque,
        image_file=image_file,
    )


def binding_references(
    service_name: str, bindings: list[ConfigBinding], existing: list[EndpointReference]
) -> list[EndpointReference]:
    """``env:`` references for bindings not already read as environment variables."""
    seen = {r.endpoint.lower() for r in existing if r.kind is ReferenceKind.ENV_VAR}
    references: list[EndpointReference] = []
    for binding in bindings:
        if binding.variable.lower() in seen:
            continue
        seen.add(binding.variable.lower())
        source = next((d.file_path for d in binding.defaults), binding.key)
        references.append(
            EndpointReference(
                service_name=service_name,
                endpoint=binding.variable,
                file_path=source,
                kind=ReferenceKind.ENV_VAR,
            )
        )
    return references


# A script setting a pipeline variable at run time: ##vso[task.setvariable variable=X]
_SET_VARIABLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"##vso\[task\.setvariable\b[^\]]*?\bvariable=([A-Za-z_][\w.-]*)", re.IGNORECASE
)
_PIPELINE_EXPRESSION_MARKERS: Final[tuple[str, ...]] = ("$(", "${{", "$[")


class PipelineCollector:
    """Variables gathered across a repository's pipeline definitions.

    ``values`` maps a name to every literal value it was given, with where;
    a name with several values, or any non-literal one, ends up opaque.
    """

    def __init__(self) -> None:
        self.values: dict[str, dict[str, set[str]]] = {}
        self.opaque: set[str] = set()
        self.opaque_sources: set[str] = set()

    def define(self, name: object, value: object, source: str) -> None:
        """Record ``name`` defined as ``value`` (opaque unless a literal)."""
        key = str(name).strip()
        if not key:
            return
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            self.opaque.add(key)
            return
        text = str(value)
        if any(marker in text for marker in _PIPELINE_EXPRESSION_MARKERS):
            self.opaque.add(key)
            return
        self.values.setdefault(key, {}).setdefault(text, set()).add(source)

    def result(self, repository: str, commit_sha: str) -> PipelineVariables:
        """Return the stored form: endpoint values kept, others by name only."""
        opaque = set(self.opaque) | {
            name for name, found in self.values.items() if len(found) > 1
        }
        variables: dict[str, str] = {}
        sources: dict[str, list[str]] = {}
        withheld: set[str] = set()
        for name, found in self.values.items():
            if name in opaque:
                continue
            (value, where), = found.items()
            kept = storable_pipeline_value(name, value)
            if kept is None:
                withheld.add(name)
            else:
                variables[name] = kept
                sources[name] = sorted(where)
        return PipelineVariables(
            repository=repository,
            commit_sha=commit_sha,
            variables=variables,
            variable_sources=sources,
            withheld_variables=sorted(withheld),
            opaque_variables=sorted(opaque),
            opaque_sources=sorted(self.opaque_sources),
        )


def collect_yaml_variables(content: str, source: str, collected: PipelineCollector) -> None:
    """Collect the variables a pipeline YAML file defines.

    ``variables`` blocks are read wherever they appear (pipeline, stage,
    job), in mapping or list form. Variable groups and templates may define
    any name, so they are opaque sources; a variable under a conditional
    (``${{ if }}``), a macro or expression value, or a name a script sets
    (``##vso[task.setvariable]``) is opaque.
    """
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        collected.opaque_sources.add(f"{source}: not valid YAML")
        return
    _walk_pipeline(document, source, collected)
    collected.opaque.update(_SET_VARIABLE_PATTERN.findall(content))


def _walk_pipeline(node: object, source: str, collected: PipelineCollector) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_pipeline(item, source, collected)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "variables":
            _variables_block(value, source, collected)
        elif key == "template" and isinstance(value, str):
            collected.opaque_sources.add(f"{source}: template {value}")
        else:
            _walk_pipeline(value, source, collected)


def _variables_block(block: object, source: str, collected: PipelineCollector) -> None:
    if isinstance(block, dict):
        for name, value in block.items():
            if str(name).startswith("${{"):
                _conditional_variables(value, source, collected)
            else:
                collected.define(name, value, source)
    elif isinstance(block, list):
        for entry in block:
            if not isinstance(entry, dict):
                continue
            if "group" in entry:
                collected.opaque_sources.add(f"{source}: variable group {entry['group']}")
            elif "template" in entry:
                collected.opaque_sources.add(f"{source}: template {entry['template']}")
            elif "name" in entry:
                collected.define(entry["name"], entry.get("value"), source)
            else:
                for name, value in entry.items():
                    if str(name).startswith("${{"):
                        _conditional_variables(value, source, collected)
    elif block is not None:
        collected.opaque_sources.add(f"{source}: variables in an unrecognized form")


def _conditional_variables(block: object, source: str, collected: PipelineCollector) -> None:
    """Variables inserted under a template condition: set or not, opaque."""
    inner = PipelineCollector()
    _variables_block(block, source, inner)
    collected.opaque.update(inner.values, inner.opaque)
    collected.opaque_sources.update(inner.opaque_sources)


def collect_definition_variables(
    definition: Any, label: str, collected: PipelineCollector
) -> None:
    """Collect variables and variable groups set on a pipeline definition.

    Secret variables and ones settable at queue time are opaque.
    """
    for name, variable in (getattr(definition, "variables", None) or {}).items():
        if getattr(variable, "is_secret", False) or getattr(variable, "allow_override", False):
            collected.opaque.add(str(name))
        else:
            collected.define(name, getattr(variable, "value", None), f"{label} (definition)")
    for group in getattr(definition, "variable_groups", None) or []:
        name = getattr(group, "name", None) or getattr(group, "id", None) or "?"
        collected.opaque_sources.add(f"{label}: variable group {name}")


_SOURCE_CLASSES: Final[frozenset[FileClass]] = frozenset(
    {FileClass.SCANNED, FileClass.UNSUPPORTED}
)


def _count(coverage: ServiceCoverage, path: str, file_class: FileClass) -> None:
    """Account for a file the scan does not read."""
    if file_class is FileClass.EXCLUDED:
        coverage.files_excluded += 1
    elif file_class is FileClass.UNSUPPORTED:
        coverage.unscanned.append(
            UnscannedFile(path=path.lstrip("/"), reason=UnscannedReason.UNSUPPORTED_LANGUAGE)
        )
    else:
        coverage.files_not_source += 1


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
    def _find_repository(git_client: Any, project: str, repo_name: str) -> Any:
        return next(
            (
                repository
                for repository in git_client.get_repositories(project)
                if repository.name == repo_name
            ),
            None,
        )

    def read_pipeline_variables(
        self, repo_name: str, revision: ScannedRevision
    ) -> PipelineVariables:
        """Return what the repository's build pipelines define.

        Read-only. Every pipeline definition bound to the repository is
        read: its YAML file at ``revision`` (see
        ``collect_yaml_variables``) and the variables and variable groups
        set on the definition. Secret and queue-time-settable definition
        variables are opaque, as are variable groups and YAML that lives in
        another repository. Never raises: when the pipelines can't be read
        the result is ``readable=False`` with the error, so no variable of
        this repository is ever treated as provably unset.
        """
        try:
            build_client, git_client, project = self._get_clients()
            repository = self._find_repository(git_client, project, repo_name)
            if repository is None:
                return PipelineVariables.unreadable(repo_name, "repository was not found")
            definitions = build_client.get_definitions(
                project=project,
                repository_id=repository.id,
                repository_type=getattr(repository, "type", None) or "TfsGit",
            )
            collected = PipelineCollector()
            for summary in definitions or []:
                definition = build_client.get_definition(
                    definition_id=summary.id, project=project
                )
                label = f"pipeline {getattr(definition, 'name', None) or summary.id}"
                self._collect_definition_yaml(
                    git_client, repository, project, revision, definition, label, collected
                )
                collect_definition_variables(definition, label, collected)
            return collected.result(repo_name, revision.commit_sha)
        except Exception as error:
            logger.warning("Cannot read pipeline variables of %s: %s", repo_name, error)
            return PipelineVariables.unreadable(repo_name, str(error))

    def _collect_definition_yaml(
        self,
        git_client: Any,
        repository: Any,
        project: str,
        revision: ScannedRevision,
        definition: Any,
        label: str,
        collected: "PipelineCollector",
    ) -> None:
        """Collect the variables a YAML pipeline's file defines at ``revision``."""
        yaml_filename = getattr(getattr(definition, "process", None), "yaml_filename", None)
        if not yaml_filename:
            return
        bound_to = getattr(getattr(definition, "repository", None), "id", None)
        if bound_to and bound_to != repository.id:
            collected.opaque_sources.add(f"{label}: YAML in another repository")
            return
        try:
            content = self._read_item_content(
                git_client, repository.id, yaml_filename, project, revision.commit_sha
            )
        except Exception as error:
            logger.warning("Cannot read %s at %s: %s", yaml_filename, revision.commit_sha, error)
            collected.opaque_sources.add(f"{label}: {yaml_filename} unreadable at the commit")
            return
        collect_yaml_variables(content, f"{label} ({yaml_filename.lstrip('/')})", collected)

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
        projects: list[BuildProject],
    ) -> list[ServiceBoundary]:
        """Return the repository's service boundaries.

        ``release-coord.yaml`` at the repository root, when present, is
        authoritative; otherwise boundaries are inferred from marker files
        and build projects.

        Raises:
            ValueError: if the manifest exists but is invalid.
        """
        if RELEASE_COORD_MANIFEST_PATH in file_paths:
            content = self._read_item_content(
                git_client, repository.id, RELEASE_COORD_MANIFEST_PATH, project, commit_sha
            )
            return _boundaries_from_manifest(parse_release_coord_manifest(content))
        return _infer_service_boundaries(file_paths, repo_name, projects)

    def _read_build_files(
        self,
        git_client: Any,
        repository_id: str,
        project: str,
        commit_sha: str,
        file_paths: list[str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Read every build and Gradle settings file outside excluded directories.

        Returns the contents by listed path and the read errors by
        repository-relative path.
        """
        dotnet_roots = dotnet_project_roots(file_paths)
        contents: dict[str, str] = {}
        failures: dict[str, str] = {}
        for path in file_paths:
            if build_file_system(path) is None and not is_gradle_settings(path):
                continue
            if classify_path(path, dotnet_roots) is FileClass.EXCLUDED:
                continue
            try:
                contents[path] = self._read_item_content(
                    git_client, repository_id, path, project, commit_sha
                )
            except Exception as error:
                logger.warning("Cannot read build file %s: %s", path, error)
                failures[path.lstrip("/")] = str(error)
        return contents, failures

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
        only the services whose root is listed are returned and read; files
        are still attributed over every boundary, exactly as a full scan.

        Service boundaries come from ``release-coord.yaml`` when the
        repository has one, otherwise from marker files and build projects
        (see ``_infer_service_boundaries``). Build files (``*.csproj``,
        ``pom.xml``, ``build.gradle``) decide ownership (see
        ``build_projects.RepositoryLayout``): each file belongs to the
        boundary with the deepest enclosing root, a shared library's files
        to every service consuming it, and test projects, unconsumed
        libraries and files outside every boundary to no service (they are
        reported, not read). References are internal, private-DNS and IP
        URLs plus endpoint-like environment variables (see
        ``_extract_references``); references read but not understood
        make a file ``REFERENCES_NOT_UNDERSTOOD``.

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
            build_contents, build_read_failures = self._read_build_files(
                git_client, repository.id, project, commit_sha, file_paths
            )
            projects, build_parse_failures = read_projects(build_contents, file_paths)
            try:
                boundaries = self._repository_boundaries(
                    git_client, repository, project, repo_name, file_paths, commit_sha, projects
                )
            except ValueError as error:
                logger.warning("Invalid release-coord.yaml in %s: %s", repo_name, error)
                return DiscoveryResult[RepositoryScan].failed(
                    f"{repo_name}: invalid release-coord.yaml: {error}"
                )
            layout = build_layout(projects, [b.root for b in boundaries])
            selected = [
                b for b in boundaries if only_roots is None or b.root in only_roots
            ]
            services = {
                b.root: _ServiceAccumulator(b, layout.libraries_of(b.root)) for b in selected
            }
            build_problems = {
                **{path: UnscannedReason.PARSE_FAILED for path in build_parse_failures},
                **{path: UnscannedReason.READ_FAILED for path in build_read_failures},
            }
            errors = [
                f"{repo_name}: cannot read {path}: {error}"
                for path, error in build_read_failures.items()
            ]
            unowned = self._read_owned_files(
                git_client,
                repository.id,
                project,
                repo_name,
                commit_sha,
                file_paths,
                layout,
                services,
                build_contents,
                build_problems,
                errors,
                list_unowned=only_roots is None,
            )
            for path, sites in unresolved_sites(projects).items():
                for root in layout.owner(path).services:
                    if root in services:
                        services[root].add_sites(path, sites)

            scans = [services[b.root].finish(repo_name, commit_sha) for b in selected]
            self._flag_clients_without_address(
                git_client, repository.id, project, commit_sha, file_paths, layout, scans, build_contents
            )
            return DiscoveryResult[RepositoryScan].from_items(
                [
                    RepositoryScan(
                        revision=revision,
                        services=scans,
                        unowned_source_files=unowned,
                        libraries=layout.shared_libraries() if only_roots is None else [],
                    )
                ],
                errors,
            )
        except Exception as error:
            logger.exception("Failed to scan repository %s", repo_name)
            return DiscoveryResult[RepositoryScan].failed(
                f"{repo_name}: repository scan failed: {error}"
            )

    def _flag_clients_without_address(
        self,
        git_client: Any,
        repository_id: str,
        project: str,
        commit_sha: str,
        file_paths: list[str],
        layout: RepositoryLayout,
        scans: list[ServiceScan],
        build_contents: dict[str, str],
    ) -> None:
        """Flag services that declare a network client but reference nothing.

        Only services whose scan found no reference and no configuration
        binding are checked, so manifests are read for those alone. Each
        client library a manifest they own declares becomes a
        ``CLIENT_WITHOUT_ADDRESS`` site on that manifest. A manifest that
        can't be read is skipped (the guard only adds flags).
        """
        silent = {
            scan.service.root: scan
            for scan in scans
            if scan.coverage is not None
            and not scan.references
            and not (scan.config and scan.config.bindings)
        }
        if not silent:
            return
        dotnet_roots = dotnet_project_roots(file_paths)
        for path in file_paths:
            if manifest_ecosystem(path) is None:
                continue
            if classify_path(path, dotnet_roots) is FileClass.EXCLUDED:
                continue
            ownership = layout.owner(path)
            owners = [silent[root] for root in ownership.services if root in silent]
            if ownership.excluded or not owners:
                continue
            try:
                content = build_contents.get(path) or self._read_item_content(
                    git_client, repository_id, path, project, commit_sha
                )
            except Exception as error:
                logger.warning("Cannot read manifest %s: %s", path, error)
                continue
            sites = [
                OpaqueReferenceSite(
                    line=library.line,
                    kind=OpaqueReferenceKind.CLIENT_WITHOUT_ADDRESS,
                    detail=library.name,
                )
                for library in client_libraries(content, path)
            ]
            for scan in owners:
                _add_unscanned_sites(scan, path.lstrip("/"), sites)

    def _read_owned_files(
        self,
        git_client: Any,
        repository_id: str,
        project: str,
        repo_name: str,
        commit_sha: str,
        file_paths: list[str],
        layout: RepositoryLayout,
        services: dict[str, "_ServiceAccumulator"],
        build_contents: dict[str, str],
        build_problems: dict[str, UnscannedReason],
        errors: list[str],
        list_unowned: bool,
    ) -> list[str]:
        """Read each file into the services that own it; return unowned source files.

        A file is read once, however many services own it (a shared
        library's). ``errors`` receives read failures.
        """
        dotnet_roots = dotnet_project_roots(file_paths)
        unowned: list[str] = []
        for path in file_paths:
            if path == RELEASE_COORD_MANIFEST_PATH:
                continue
            relative = path.lstrip("/")
            file_class = classify_path(path, dotnet_roots)
            ownership = layout.owner(path)
            if ownership.excluded:
                continue
            if not ownership.services:
                if list_unowned and (file_class in _SOURCE_CLASSES or relative in build_problems):
                    unowned.append(relative)
                continue
            owners = [services[root] for root in ownership.services if root in services]
            if not owners:
                continue
            problem = build_problems.get(relative)
            if problem is not None:
                for owner in owners:
                    owner.unscanned(relative, problem)
                continue
            if file_class is not FileClass.SCANNED:
                for owner in owners:
                    _count(owner.coverage, path, file_class)
                continue
            try:
                content = build_contents.get(path)
                if content is None:
                    content = self._read_item_content(
                        git_client, repository_id, path, project, commit_sha
                    )
            except Exception as error:
                logger.warning("Cannot read %s in %s: %s", path, repo_name, error)
                errors.append(f"{repo_name}: cannot read {path}: {error}")
                for owner in owners:
                    owner.unscanned(relative, UnscannedReason.READ_FAILED)
                continue
            try:
                read = read_source_file(content, owners[0].boundary.name, path)
            except ConfigParseError as error:
                logger.warning("Cannot parse %s in %s: %s", path, repo_name, error)
                for owner in owners:
                    owner.unscanned(relative, UnscannedReason.PARSE_FAILED)
                continue
            for owner in owners:
                owner.add_read(path, read, from_library=ownership.library is not None)
        return unowned


def _add_unscanned_sites(scan: ServiceScan, path: str, sites: list[OpaqueReferenceSite]) -> None:
    """Record not-understood ``sites`` in ``path`` on a finished scan."""
    if not sites or scan.coverage is None:
        return
    for unscanned in scan.coverage.unscanned:
        if unscanned.path == path and unscanned.reason is UnscannedReason.REFERENCES_NOT_UNDERSTOOD:
            unscanned.sites.extend(sites)
            return
    scan.coverage.unscanned.append(
        UnscannedFile(path=path, reason=UnscannedReason.REFERENCES_NOT_UNDERSTOOD, sites=list(sites))
    )


class _ServiceAccumulator:
    """What one service's files contribute while its repository is read."""

    def __init__(self, boundary: ServiceBoundary, libraries: list[str]) -> None:
        self.boundary = boundary
        self.coverage = ServiceCoverage(
            service_name=boundary.name, root=boundary.root, libraries=libraries
        )
        self.references: dict[tuple[ReferenceKind, str], EndpointReference] = {}
        self.bindings: list[ConfigBinding] = []
        self.images: list[tuple[str, ImageEnvironment]] = []
        self.sites: dict[str, list[OpaqueReferenceSite]] = {}
        self.sections: dict[str, list[BoundSection]] = {}
        self.keys: dict[ConfigFramework, set[str]] = {}

    def unscanned(self, path: str, reason: UnscannedReason) -> None:
        """Record a file whose references are unknown."""
        self.coverage.unscanned.append(UnscannedFile(path=path, reason=reason))

    def add_sites(self, path: str, sites: list[OpaqueReferenceSite]) -> None:
        """Record references in ``path`` that weren't understood."""
        if sites:
            self.sites.setdefault(path.lstrip("/"), []).extend(sites)

    def add_read(self, path: str, read: FileRead, from_library: bool) -> None:
        """Add one scanned file. A library's Dockerfile doesn't build this image."""
        relative = path.lstrip("/")
        self.coverage.files_scanned += 1
        self.bindings.extend(read.bindings)
        if read.image is not None and not from_library:
            self.images.append((relative, read.image))
        for reference in read.references:
            if reference.service_name != self.boundary.name:
                reference = reference.model_copy(update={"service_name": self.boundary.name})
            self.references.setdefault((reference.kind, reference.endpoint), reference)
        self.add_sites(relative, read.sites)
        if read.sections:
            self.sections.setdefault(relative, []).extend(read.sections)
        for framework, keys in read.config_keys.items():
            self.keys.setdefault(framework, set()).update(keys)

    def finish(self, repository: str, commit_sha: str) -> ServiceScan:
        """Return the service's scan, with bound sections checked against its keys."""
        for path, sections in self.sections.items():
            self.add_sites(path, unbound_section_sites(sections, self.keys))
        for path, sites in self.sites.items():
            unique = {(site.line, site.kind, site.detail): site for site in sites}
            self.coverage.unscanned.append(
                UnscannedFile(
                    path=path,
                    reason=UnscannedReason.REFERENCES_NOT_UNDERSTOOD,
                    sites=[unique[key] for key in sorted(unique, key=lambda k: (k[0], k[1].value, k[2]))],
                )
            )
        config = build_static_config(repository, self.boundary, commit_sha, self.bindings, self.images)
        found = list(self.references.values())
        found.extend(binding_references(self.boundary.name, config.bindings, found))
        return ServiceScan(
            service=self.boundary, references=found, coverage=self.coverage, config=config
        )

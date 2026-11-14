"""Core domain models for the enterprise service dependency graph."""

import re
from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Dict, Final, Generic, List, Literal, Optional, Self, TypeVar
from urllib.parse import urlsplit

from src.core.env_names import (
    PROFILE_VARIABLES,
    is_connection_string_variable,
    is_endpoint_variable,
    storable_endpoint_value,
)
from pydantic import BaseModel, Field, field_validator, model_validator

from src.core.proto_contract import ContractFinding
from src.core.telemetry_capability import TelemetryStack


T = TypeVar("T")


class DiscoveryMethod(Enum):
    """Source used to discover a dependency between services.

    HUMAN_DECLARED is a dependency a coordinator declared in the project's
    dependency review: it counts for the blast radius and deployment order,
    but is no automated evidence source, so no discovery stage reports it
    and a policy can never require it.
    """

    STATIC_CODE = "STATIC_CODE"
    RUNTIME_TELEMETRY = "RUNTIME_TELEMETRY"
    K8S_MANIFEST = "K8S_MANIFEST"
    HUMAN_DECLARED = "HUMAN_DECLARED"


class DiscoveryStatus(str, Enum):
    """Outcome of one extraction call or discovery stage.

    SUCCESS: every requested source was read completely.
    PARTIAL: some data was read, but part of the source failed or was dropped.
    FAILED: the source could not be read; any data is unusable.
    SKIPPED: the source was not configured, so nothing was attempted.
    """

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

    @property
    def is_incomplete(self) -> bool:
        """Whether this outcome means the evidence cannot be trusted as complete."""
        return self in (DiscoveryStatus.PARTIAL, DiscoveryStatus.FAILED)


def combine_statuses(statuses: Iterable[DiscoveryStatus]) -> DiscoveryStatus:
    """Fold per-call statuses into one stage status.

    All SKIPPED (or none) is SKIPPED; all attempted calls succeeding is
    SUCCESS; all attempted calls failing is FAILED; any other mix is PARTIAL.
    """
    attempted = [s for s in statuses if s is not DiscoveryStatus.SKIPPED]
    if not attempted:
        return DiscoveryStatus.SKIPPED
    if all(s is DiscoveryStatus.SUCCESS for s in attempted):
        return DiscoveryStatus.SUCCESS
    if all(s is DiscoveryStatus.FAILED for s in attempted):
        return DiscoveryStatus.FAILED
    return DiscoveryStatus.PARTIAL


class DiscoveryResult(BaseModel, Generic[T]):
    """Explicit outcome of an extraction call, replacing a bare (maybe empty) list.

    An empty ``items`` list only means "nothing exists" when ``status`` is
    SUCCESS. FAILED and SKIPPED results carry no items, and PARTIAL and
    FAILED results must say what went wrong in ``errors``.
    """

    status: DiscoveryStatus
    items: List[T] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.status.is_incomplete and not self.errors:
            raise ValueError(f"{self.status.value} results must include errors")
        if self.status in (DiscoveryStatus.FAILED, DiscoveryStatus.SKIPPED):
            if self.items:
                raise ValueError(f"{self.status.value} results must not carry items")
        return self

    @classmethod
    def success(cls, items: List[T]) -> Self:
        """Build a complete result."""
        return cls(status=DiscoveryStatus.SUCCESS, items=items)

    @classmethod
    def from_items(cls, items: List[T], errors: List[str]) -> Self:
        """Build SUCCESS when there are no errors, otherwise PARTIAL."""
        if not errors:
            return cls.success(items)
        return cls(status=DiscoveryStatus.PARTIAL, items=items, errors=errors)

    @classmethod
    def failed(cls, error: str) -> Self:
        """Build a failed result that carries no usable items."""
        return cls(status=DiscoveryStatus.FAILED, errors=[error])

    @classmethod
    def skipped(cls) -> Self:
        """Build a result for a source that was not configured."""
        return cls(status=DiscoveryStatus.SKIPPED)


class DiscoveryStageReport(BaseModel):
    """Outcome of one discovery stage, persisted alongside the knowledge graph."""

    source: DiscoveryMethod
    status: DiscoveryStatus
    item_count: int = Field(ge=0, default=0)
    errors: List[str] = Field(default_factory=list)
    # Records deliberately left out as noise, counted per reason (e.g.
    # telemetry loopback calls). Not errors: they don't make a stage PARTIAL.
    dropped_records: Dict[str, int] = Field(default_factory=dict)


MOCK_FIXTURE_PROVENANCE: Final[str] = "mock_fixture"
UNKNOWN_LABEL: Final[str] = "unknown"
LABEL_SEPARATOR: Final[str] = ","

FQURI_PREFIX: Final[str] = "urn:release-coord"
# One FQURI segment: no colons, so the URI splits unambiguously.
IDENTITY_SEGMENT_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_FQURI_SEGMENT_COUNT: Final[int] = 5


class ServiceIdentity(BaseModel, frozen=True):
    """Fully qualified identity of a service.

    Rendered as the FQURI
    ``urn:release-coord:{tenant}:{environment}:{cluster}:{namespace}:{service_name}``.
    Segments that discovery could not determine (for example environment and
    cluster during static extraction) are ``unknown``; an identity with
    unknown segments can still be used as a query that matches any value in
    those positions (see ``matches``).
    """

    tenant: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
    environment: str = Field(default=UNKNOWN_LABEL, pattern=IDENTITY_SEGMENT_PATTERN)
    cluster: str = Field(default=UNKNOWN_LABEL, pattern=IDENTITY_SEGMENT_PATTERN)
    namespace: str = Field(default=UNKNOWN_LABEL, pattern=IDENTITY_SEGMENT_PATTERN)
    service_name: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)

    @property
    def uri(self) -> str:
        """Return the FQURI form of this identity."""
        return ":".join(
            [
                FQURI_PREFIX,
                self.tenant,
                self.environment,
                self.cluster,
                self.namespace,
                self.service_name,
            ]
        )

    @classmethod
    def parse(cls, uri: str) -> Self:
        """Parse a FQURI.

        Raises:
            ValueError: if ``uri`` is not a well-formed FQURI (a pydantic
                ValidationError, itself a ValueError, for invalid segments).
        """
        prefix = f"{FQURI_PREFIX}:"
        if not uri.startswith(prefix):
            raise ValueError(f"not a {FQURI_PREFIX} URI: {uri!r}")
        segments = uri[len(prefix) :].split(":")
        if len(segments) != _FQURI_SEGMENT_COUNT:
            raise ValueError(
                f"FQURI must have {_FQURI_SEGMENT_COUNT} segments after the "
                f"prefix: {uri!r}"
            )
        tenant, environment, cluster, namespace, service_name = segments
        return cls(
            tenant=tenant,
            environment=environment,
            cluster=cluster,
            namespace=namespace,
            service_name=service_name,
        )

    def matches(self, candidate: "ServiceIdentity") -> bool:
        """Whether ``candidate`` fits this identity used as a query.

        Tenant and service name must be equal. Environment, cluster and
        namespace must be equal unless this identity leaves them unknown.
        """
        if (self.tenant, self.service_name) != (
            candidate.tenant,
            candidate.service_name,
        ):
            return False
        return all(
            mine == UNKNOWN_LABEL or mine == theirs
            for mine, theirs in [
                (self.environment, candidate.environment),
                (self.cluster, candidate.cluster),
                (self.namespace, candidate.namespace),
            ]
        )


def is_fquri(value: str) -> bool:
    """Whether ``value`` is a well-formed service FQURI."""
    try:
        ServiceIdentity.parse(value)
    except ValueError:
        return False
    return True


# ServiceNode.metadata keys recorded by static-code discovery: the source
# repository a service lives in, and its repository-relative root directory
# ("" for the repository root). Used to map changed files to services.
REPOSITORY_METADATA_KEY: Final[str] = "repository"
SERVICE_ROOT_METADATA_KEY: Final[str] = "service_root"
# How a source-code service was tied to its runtime counterpart (see
# ``identity_links``), or that no runtime service matched it.
IDENTITY_LINK_METADATA_KEY: Final[str] = "identity_link"
IDENTITY_UNLINKED_METADATA_KEY: Final[str] = "identity_unlinked"
IDENTITY_SUGGESTIONS_METADATA_KEY: Final[str] = "identity_suggestions"


class DeployingPipeline(BaseModel):
    """The Azure Pipelines run that last applied a workload.

    Read from the ``azure-pipelines/*`` annotations the KubernetesManifest
    task stamps on the objects it deploys; any field the cluster doesn't
    carry is None.
    """

    organization: str | None = None
    project: str | None = None
    pipeline: str | None = None
    pipeline_id: str | None = None
    run_id: str | None = None
    run_url: str | None = None
    job: str | None = None


class DeployedWorkload(BaseModel):
    """What one Kubernetes workload of a service runs, as the cluster says.

    ``workload`` is ``<kind>/<name>``; ``images`` its containers' images
    (sorted, unique); ``pipeline`` the run that deployed it, when annotated.
    """

    workload: str = Field(min_length=1)
    images: List[str] = Field(default_factory=list)
    pipeline: DeployingPipeline | None = None


def merge_deployed_workloads(
    first: List[DeployedWorkload] | None, second: List[DeployedWorkload] | None
) -> List[DeployedWorkload] | None:
    """Union two nodes' deployed workloads by workload (first wins); None if neither has any."""
    merged = {workload.workload: workload for workload in [*(second or []), *(first or [])]}
    return [merged[name] for name in sorted(merged)] or None


class ServiceNode(BaseModel):
    """A microservice node and its identifying metadata in the dependency graph.

    ``service_id`` is the service's FQURI (see ``ServiceIdentity``).
    ``deployed_workloads`` is what the cluster runs for it (images, and the
    pipeline run that deployed it); None when discovery didn't read the
    cluster for this service, or the snapshot predates it. Criticality is
    derived, not stored (see ``orchestration.inventory``); snapshots that
    still carry the old ``criticality_tier`` load with it ignored.
    """

    service_id: str
    name: str
    repository_url: Optional[str] = None
    metadata: Dict[str, str] = Field(default_factory=dict)
    deployed_workloads: List[DeployedWorkload] | None = None

    @field_validator("service_id")
    @classmethod
    def _require_fquri(cls, value: str) -> str:
        ServiceIdentity.parse(value)
        return value

    @property
    def identity(self) -> ServiceIdentity:
        """Return the parsed identity of this service."""
        return ServiceIdentity.parse(self.service_id)


class ServiceEnvironment(BaseModel):
    """Environment variables Kubernetes injects into a service's workloads.

    ``service_id`` is the FQURI of the Kubernetes Service whose selector
    matches the workloads; ``variables`` maps each variable name to its
    resolved value (literal ``value`` or ``configMapKeyRef``). Variables
    whose value could not be determined unambiguously are left out.

    ``opaque_variables`` are names that are set but whose value can't be
    read (Secret or field references, missing config maps, conflicting
    values, or a config map value a later ``envFrom`` Secret may override),
    and a name any container sets opaquely is never in ``variables``.
    ``variable_sources`` records where each readable value came from, as
    ``<kind>/<workload>/<container>:<source>`` entries. ``has_opaque_sources`` is true when a workload imports a whole
    Secret or a config map that couldn't be read (``envFrom``), so any name
    may be set. ``withheld_variables`` are readable names whose values a
    stored snapshot leaves out (see ``for_storage``). Together they let a
    variable be proven unset: absent from all of them.
    """

    service_id: str
    variables: Dict[str, str] = Field(default_factory=dict)
    variable_sources: Dict[str, List[str]] = Field(default_factory=dict)
    opaque_variables: List[str] = Field(default_factory=list)
    withheld_variables: List[str] = Field(default_factory=list)
    has_opaque_sources: bool = False

    def is_unset(self, variable: str) -> bool:
        """Whether ``variable`` is provably not set for this service."""
        return not (
            self.has_opaque_sources
            or variable in self.variables
            or variable in self.opaque_variables
            or variable in self.withheld_variables
        )

    def for_storage(self) -> "ServiceEnvironment":
        """Return the environment safe to write into a baseline snapshot.

        Only endpoint-like variables (the ones dependency resolution reads)
        keep a value, with credentials stripped (see
        ``storable_endpoint_value``; a connection string keeps only its
        server), and so do the profile selectors in ``PROFILE_VARIABLES``.
        Every other readable variable, and an endpoint whose value still
        carries a credential, is kept by name only in
        ``withheld_variables``, so it still counts as set.
        """
        kept: dict[str, str] = {}
        withheld = set(self.withheld_variables)
        for name, value in self.variables.items():
            cleaned = storable_variable_value(name, value)
            if cleaned is None:
                withheld.add(name)
            else:
                kept[name] = cleaned
        return self.model_copy(
            update={
                "variables": kept,
                "variable_sources": {
                    name: sources for name, sources in self.variable_sources.items() if name in kept
                },
                "withheld_variables": sorted(withheld),
            }
        )

    @field_validator("service_id")
    @classmethod
    def _require_fquri(cls, value: str) -> str:
        ServiceIdentity.parse(value)
        return value


class UnresolvedEntityKind(str, Enum):
    """Why a discovered endpoint could not be mapped to a registered service.

    IP_ADDRESS: the endpoint is an IP address.
    PRIVATE_DNS: a private or internal DNS name outside the cluster.
    EXTERNAL_DNS: a public DNS name.
    UNREGISTERED_SERVICE: looks like a service name, but no registered
        service in the inventory matches it (exactly one match is required).
    ENVIRONMENT_VARIABLE: an address read from an environment variable
        (``env:<service>/<VARIABLE>``) whose value is not yet known.
    ENVIRONMENT_VARIABLE_UNSET: an address read from an environment
        variable that the service's Kubernetes workloads provably don't set,
        so the code's built-in default is used. Live telemetry shows where
        that default points; without live evidence it blocks like any gap.
    AMBIGUOUS_SERVICE: source code declares a service that cannot be tied
        to exactly one identity: two repository directories claim the same
        service, or a service with no namespace matches services in several
        scanned namespaces. Declaring ``namespace`` in ``release-coord.yaml``
        resolves it.
    """

    IP_ADDRESS = "IP_ADDRESS"
    PRIVATE_DNS = "PRIVATE_DNS"
    EXTERNAL_DNS = "EXTERNAL_DNS"
    UNREGISTERED_SERVICE = "UNREGISTERED_SERVICE"
    ENVIRONMENT_VARIABLE = "ENVIRONMENT_VARIABLE"
    AMBIGUOUS_SERVICE = "AMBIGUOUS_SERVICE"
    ENVIRONMENT_VARIABLE_UNSET = "ENVIRONMENT_VARIABLE_UNSET"


UNRESOLVED_ID_PREFIX: Final[str] = "unresolved:"


class UnresolvedEntity(BaseModel):
    """A dependency endpoint that does not map to a registered service.

    Edges point at ``entity_id`` (``unresolved:<reference>``), which can
    never collide with a service FQURI. ``reference`` is the endpoint as it
    was discovered, normalized to lower case.
    """

    entity_id: str = Field(min_length=len(UNRESOLVED_ID_PREFIX) + 1)
    reference: str = Field(min_length=1)
    kind: UnresolvedEntityKind

    @model_validator(mode="after")
    def _id_matches_reference(self) -> Self:
        if self.entity_id != unresolved_entity_id(self.reference):
            raise ValueError(
                f"entity_id must be {UNRESOLVED_ID_PREFIX!r} + reference"
            )
        return self


def unresolved_entity_id(reference: str) -> str:
    """Return the node ID used for an unresolved ``reference``."""
    return f"{UNRESOLVED_ID_PREFIX}{reference}"


class DeploymentContext(BaseModel):
    """Runtime deployment state associated with a service in the dependency graph."""

    environment: str
    cluster_name: str
    namespace: str
    active_image_tag: str


REQUIRED_STAGE_SOURCES = (
    DiscoveryMethod.STATIC_CODE,
    DiscoveryMethod.K8S_MANIFEST,
    DiscoveryMethod.RUNTIME_TELEMETRY,
)


def baseline_problems(reports: list[DiscoveryStageReport]) -> list[str]:
    """Return why ``reports`` cannot back a baseline; empty when they can.

    Every extraction stage must have reported, none may be FAILED or PARTIAL,
    and at least one must have actually run.
    """
    by_source = {report.source: report for report in reports}
    problems: list[str] = []
    for source in REQUIRED_STAGE_SOURCES:
        report = by_source.get(source)
        if report is None:
            problems.append(f"{source.value} stage did not report an outcome")
        elif report.status.is_incomplete:
            detail = "; ".join(report.errors) or "no details"
            problems.append(f"{source.value} {report.status.value}: {detail}")
    if not problems and all(
        report.status is DiscoveryStatus.SKIPPED for report in by_source.values()
    ):
        problems.append("every discovery stage was skipped")
    return problems


class DiscoveryIncompleteError(RuntimeError):
    """Raised when discovery evidence is too incomplete to become a baseline."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            "Baseline not established; discovery is incomplete: "
            + "; ".join(problems)
        )
        self.problems = problems


GIT_COMMIT_SHA_PATTERN: Final[str] = r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"


class ScannedRevision(BaseModel, frozen=True):
    """The exact commit discovery read one repository at.

    ``branch`` is the ref the commit was resolved from (the repository's
    default branch); ``commit_sha`` is a full, lower-case SHA-1 or SHA-256.
    """

    repository: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    commit_sha: str = Field(pattern=GIT_COMMIT_SHA_PATTERN)


class UnscannedReason(str, Enum):
    """Why a source file in a service directory was not scanned.

    UNSUPPORTED_LANGUAGE: source code in a language the scanner can't read
        (Rust, Kotlin, C++...); a dependency it declares is invisible.
    READ_FAILED: the file couldn't be read at the scanned commit.
    PARSE_FAILED: a framework configuration file (appsettings*.json,
        application*.yml/.properties) or a build file (*.csproj, pom.xml)
        couldn't be parsed, so its keys or project references are unknown.
    REFERENCES_NOT_UNDERSTOOD: the file was read, but some references in
        it can't be followed (see ``OpaqueReferenceSite``), so a dependency
        it declares there is invisible.
    """

    UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
    READ_FAILED = "READ_FAILED"
    PARSE_FAILED = "PARSE_FAILED"
    REFERENCES_NOT_UNDERSTOOD = "REFERENCES_NOT_UNDERSTOOD"


class OpaqueReferenceKind(str, Enum):
    """A reference the scanner read but can't follow.

    DYNAMIC_CONFIG_KEY: a configuration read whose key isn't a literal
        (``configuration[name]``, ``GetSection($"...")``, ``getProperty(k)``).
    DYNAMIC_ENV_NAME: an environment read whose name isn't a literal.
    CLIENT_ADDRESS_EXPRESSION: an HTTP/gRPC client address that is neither
        a literal nor a configuration or environment read (``BaseAddress``,
        ``GrpcChannel.ForAddress``, ``WebClient.baseUrl``...).
    UNBOUND_SECTION: a configuration section bound to an options class
        (``GetSection("X")``, ``@ConfigurationProperties("x")``) with no
        keys in the service's configuration files, so its keys are unknown.
    PROJECT_REFERENCE_UNRESOLVED: a project reference that doesn't resolve
        to a project in the repository, so the code it pulls in is unread.
    CLIENT_WITHOUT_ADDRESS: the service declares a network client library
        (HTTP, gRPC, messaging, database, service registry, feature-flag
        SDK) but the scan found no address it calls, so its dependencies
        are configured somewhere the scanner doesn't read; ``detail`` names
        the library.
    EXTERNAL_CONFIG_SOURCE: configuration is imported from a source outside
        the repository (Spring Cloud Config ``spring.config.import:
        configserver:``), so keys served there are unknown.
    """

    DYNAMIC_CONFIG_KEY = "DYNAMIC_CONFIG_KEY"
    DYNAMIC_ENV_NAME = "DYNAMIC_ENV_NAME"
    CLIENT_ADDRESS_EXPRESSION = "CLIENT_ADDRESS_EXPRESSION"
    UNBOUND_SECTION = "UNBOUND_SECTION"
    PROJECT_REFERENCE_UNRESOLVED = "PROJECT_REFERENCE_UNRESOLVED"
    CLIENT_WITHOUT_ADDRESS = "CLIENT_WITHOUT_ADDRESS"
    EXTERNAL_CONFIG_SOURCE = "EXTERNAL_CONFIG_SOURCE"


class OpaqueReferenceSite(BaseModel, frozen=True):
    """Where in a file a reference wasn't understood.

    ``detail`` names the section or project reference involved (never
    code or values, which may hold credentials); blank otherwise.
    """

    line: int = Field(ge=1)
    kind: OpaqueReferenceKind
    detail: str = ""


class UnscannedFile(BaseModel):
    """A source file whose dependencies the scanner could not see.

    ``sites`` lists, for REFERENCES_NOT_UNDERSTOOD, each reference that
    wasn't understood.
    """

    path: str = Field(min_length=1)
    reason: UnscannedReason
    sites: List[OpaqueReferenceSite] = Field(default_factory=list)


class ContractRole(str, Enum):
    """How a service uses a contract file (a .NET ``<Protobuf GrpcServices>``).

    CLIENT: it calls the service the contract describes.
    SERVER: it implements that service.
    BOTH: both (Grpc.Tools' default when ``GrpcServices`` is absent).
    """

    CLIENT = "CLIENT"
    SERVER = "SERVER"
    BOTH = "BOTH"

    @property
    def calls(self) -> bool:
        return self is not ContractRole.SERVER

    @property
    def serves(self) -> bool:
        return self is not ContractRole.CLIENT


class ContractLink(BaseModel):
    """One contract file a service's build compiles: ``path`` is
    repository-relative, ``project_file`` the build file declaring it."""

    path: str = Field(min_length=1)
    role: ContractRole
    project_file: str = ""


class ServiceCoverage(BaseModel):
    """How completely one service directory was scanned at one commit.

    ``root`` is the service's repository-relative directory (as in the
    service node's metadata). Counts are files under that directory the
    service owns: ``files_scanned`` were read for references,
    ``files_excluded`` were skipped by design (vendored dependencies, build
    output, documentation directories, lock files), ``files_not_source``
    are not code or configuration (images, Markdown...). ``unscanned`` are
    source files whose references are unknown; any makes coverage
    incomplete. Container-image defaults (Dockerfile ``ENV``) are not
    source files here; see G6 in docs/release-model.md. ``libraries`` are
    the roots of the shared-library projects the service consumes
    (directly or transitively); their files count here too. ``contracts``
    are the contract files its build compiles (see ``ContractLink``).
    """

    service_name: str = Field(min_length=1)
    root: str
    files_scanned: int = Field(ge=0, default=0)
    files_excluded: int = Field(ge=0, default=0)
    files_not_source: int = Field(ge=0, default=0)
    unscanned: List[UnscannedFile] = Field(default_factory=list)
    libraries: List[str] = Field(default_factory=list)
    contracts: List[ContractLink] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether every source file the service owns was scanned."""
        return not self.unscanned


class SharedLibrary(BaseModel):
    """A library project whose code ships inside the services that consume it.

    ``root`` is its repository-relative directory, ``project_file`` the
    build file declaring it, and ``consumers`` the roots of the services
    that reference it (directly or through other libraries). A change to
    its files changes each consumer.
    """

    root: str
    project_file: str = Field(min_length=1)
    consumers: List[str] = Field(default_factory=list)


class SharedFile(BaseModel):
    """A file outside every service directory that services copy into their images.

    ``path`` is repository-relative; ``consumers`` are the roots of the
    services whose Dockerfiles ``copied_by`` copy it (``COPY``/``ADD``, see
    ``tools.docker_copies``). Like a shared library's file, a change to it
    changes each consumer.
    """

    path: str = Field(min_length=1)
    consumers: List[str] = Field(default_factory=list)
    copied_by: List[str] = Field(default_factory=list)


class RepositoryCoverage(BaseModel):
    """Scan coverage for every service in one repository at one commit.

    ``unowned_source_files`` are source files outside every service
    directory and every consumed library (tooling, test projects); they
    are reported, not read. ``libraries`` are the consumed shared libraries,
    ``shared_files`` the files outside every service that Dockerfiles copy in.
    """

    repository: str = Field(min_length=1)
    commit_sha: str = Field(pattern=GIT_COMMIT_SHA_PATTERN)
    services: List[ServiceCoverage] = Field(default_factory=list)
    unowned_source_files: List[str] = Field(default_factory=list)
    libraries: List[SharedLibrary] = Field(default_factory=list)
    shared_files: List[SharedFile] = Field(default_factory=list)


class PipelineVariables(BaseModel):
    """What a repository's build pipelines define, as read at one commit.

    Pipelines never decide a dependency on their own: the live cluster's
    container environment already reflects any deploy-time substitution.
    They matter because a pipeline variable can be baked into the image at
    build time, so a variable Kubernetes doesn't set is not provably unset
    when a pipeline may define it.

    ``variables`` keeps values only for endpoint-like names, credentials
    stripped (as ``ServiceEnvironment.for_storage``); every other readable
    name is in ``withheld_variables``. ``opaque_variables`` are defined
    with a value we can't know (secret, settable at queue time, an
    expression or macro, set by a script). ``opaque_sources`` describe what
    may define any name (variable groups, templates, YAML in another
    repository). ``readable`` is false when the pipelines couldn't be read
    at all (``error`` says why); then any name may be defined.
    ``variable_sources`` names where each kept value was defined.
    """

    repository: str = Field(min_length=1)
    commit_sha: str = ""
    readable: bool = True
    error: str = ""
    variables: Dict[str, str] = Field(default_factory=dict)
    variable_sources: Dict[str, List[str]] = Field(default_factory=dict)
    withheld_variables: List[str] = Field(default_factory=list)
    opaque_variables: List[str] = Field(default_factory=list)
    opaque_sources: List[str] = Field(default_factory=list)

    @classmethod
    def unreadable(cls, repository: str, error: str) -> "PipelineVariables":
        """Record that the repository's pipelines couldn't be read."""
        return cls(repository=repository, readable=False, error=error or "unreadable")

    def may_define(self, name: str) -> bool:
        """Whether a pipeline defines ``name``, or might."""
        return (
            not self.readable
            or bool(self.opaque_sources)
            or name in self.variables
            or name in self.withheld_variables
            or name in self.opaque_variables
        )

    def describe(self, name: str) -> str:
        """Say how the pipelines may define ``name`` (call when ``may_define``)."""
        if not self.readable:
            return f"the pipelines of {self.repository} couldn't be read ({self.error})"
        if name in self.variables:
            sources = ", ".join(self.variable_sources.get(name, [])) or "a pipeline"
            return f"{sources} in {self.repository} sets {name}={self.variables[name]}"
        if name in self.withheld_variables or name in self.opaque_variables:
            return f"a pipeline in {self.repository} sets {name} (value not readable)"
        return (
            f"pipelines in {self.repository} import variables we can't read ("
            + ", ".join(self.opaque_sources)
            + ")"
        )


def storable_variable_value(name: str, value: str) -> str | None:
    """Return the value a snapshot may keep for an environment variable, or None.

    Endpoint variables keep their credential-free endpoint; profile
    selectors (``PROFILE_VARIABLES``) keep their value; anything else None.
    """
    if name.upper() in PROFILE_VARIABLES:
        return value.strip()
    if not is_endpoint_variable(name):
        return None
    return storable_endpoint_value(value, connection_string=is_connection_string_variable(name))


def storable_pipeline_value(name: str, value: str) -> str | None:
    """Return the value a snapshot may keep for a pipeline variable, or None."""
    return storable_variable_value(name, value)


class ConfigFramework(str, Enum):
    """How a configuration key maps onto environment variables.

    ENV: the key is an environment variable name, matched exactly.
    DOTNET: Microsoft.Extensions.Configuration: ``A:B`` is overridden by
        ``A__B``, matched case-insensitively; ``ASPNETCORE_ENVIRONMENT`` or
        ``DOTNET_ENVIRONMENT`` (default ``Production``) picks
        ``appsettings.<Environment>.json``.
    SPRING: Spring Boot: ``a.b-c`` is overridden by ``A_BC`` (relaxed
        binding); ``${NAME:default}`` placeholders read other properties or
        variables; ``SPRING_PROFILES_ACTIVE`` picks ``application-<profile>``.
    """

    ENV = "ENV"
    DOTNET = "DOTNET"
    SPRING = "SPRING"


class StaticDefaultSource(str, Enum):
    """Where a default for a configuration key was written.

    CONFIG_FILE: a framework configuration file (appsettings*.json,
        application*.yml/.properties); ``profile`` names its profile.
    CODE: the code that reads it (``os.getenv("X", "http://y")``,
        ``@Value("${x:http://y}")``, ``config["X"] ?? "http://y"``).
    """

    CONFIG_FILE = "CONFIG_FILE"
    CODE = "CODE"


class StaticDefault(BaseModel):
    """One default value for a configuration key, as source control holds it.

    ``template`` is credential-free: an endpoint (see ``endpoint_of_value``)
    or, for Spring, a value whose ``${NAME:default}`` placeholders are
    filled in at resolution time, with user info and query removed.
    """

    template: str = Field(min_length=1)
    source: StaticDefaultSource
    file_path: str = Field(min_length=1)
    profile: Optional[str] = None


class ConfigBinding(BaseModel):
    """A configuration key a service reads as a dependency address.

    ``variable`` is the environment variable that overrides the key (the
    name its ``env:`` reference cites); ``key`` is the key as the framework
    writes it (``ConnectionStrings:EventBus``, ``spring.datasource.url``).
    ``defaults`` are the values the service falls back to when nothing in
    its runtime environment sets the key (see ``config_resolution``).
    """

    variable: str = Field(min_length=1)
    key: str = Field(min_length=1)
    framework: ConfigFramework
    defaults: List[StaticDefault] = Field(default_factory=list)

    def names(self, variable: str) -> bool:
        """Whether ``variable`` is this binding's override variable."""
        if self.framework is ConfigFramework.DOTNET:
            return variable.lower() == self.variable.lower()
        return variable == self.variable


class ServiceStaticConfig(BaseModel):
    """Everything source control says a service's configuration defaults to.

    Read at one commit for one service directory. ``bindings`` are the keys
    it reads as dependency addresses. ``image_variables`` are the container
    image's ``ENV`` values (from the service's Dockerfile, final stage and
    the stages it builds on) a snapshot may keep: endpoints without
    credentials and profile selectors; ``image_withheld_variables`` are
    other names the image sets, ``image_opaque_variables`` names whose value
    comes from a build argument or expansion. Base images pulled from a
    registry are not read.
    """

    repository: str = Field(min_length=1)
    root: str
    service_name: str = Field(min_length=1)
    commit_sha: str = ""
    bindings: List[ConfigBinding] = Field(default_factory=list)
    image_variables: Dict[str, str] = Field(default_factory=dict)
    image_withheld_variables: List[str] = Field(default_factory=list)
    image_opaque_variables: List[str] = Field(default_factory=list)
    image_file: Optional[str] = None

    def binding(self, variable: str) -> Optional[ConfigBinding]:
        """Return the binding ``variable`` overrides, if any."""
        return next((b for b in self.bindings if b.names(variable)), None)


class StaticDefaultOrigin(str, Enum):
    """Which static layer supplied the value a dependency was resolved with.

    IMAGE_ENV: the container image's Dockerfile ``ENV``.
    CONFIG_FILE / CODE: as ``StaticDefaultSource``.
    """

    IMAGE_ENV = "IMAGE_ENV"
    CONFIG_FILE = "CONFIG_FILE"
    CODE = "CODE"


class StaticDefaultUse(BaseModel):
    """A dependency resolved from a static default, not the runtime environment.

    Recorded only when Kubernetes, the build pipelines and every other
    higher-precedence source provably don't set the variable, so the
    service really runs with this value. ``endpoint`` is the
    credential-free value; ``target`` the node it resolved to.
    """

    service_id: str = Field(min_length=1)
    variable: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    origin: StaticDefaultOrigin
    file_path: str = Field(min_length=1)
    profile: Optional[str] = None
    target: str = Field(min_length=1)


class ShapeFindingKind(str, Enum):
    """A namespace object outside the supported application shape.

    UNEXPOSED_WORKLOAD: a long-running workload (or CronJob) no Service
        selects, such as a queue worker; registered as a service so its
        calls count, with ``exposed=false``.
    TRANSIENT_WORKLOAD: a bare Job or Pod no Service selects; not
        registered, since it comes and goes.
    UNREAD_CONTROLLER: pods owned by a controller kind discovery doesn't
        read (Argo Rollouts, operators); their running pod specs are read
        instead, so a scaled-to-zero workload is invisible.
    SELECTORLESS_SERVICE: a Service without a selector; its backends are
        managed outside Kubernetes selectors and their environment is unknown.
    SERVICE_SELECTS_NO_WORKLOAD: a Service whose selector matches no
        workload read; its environment is unknown.
    EXTERNAL_NAME_SERVICE: a DNS alias to ``externalName``; calls to it are
        calls to that host.
    NAME_COLLISION: an unexposed workload has the name of a Service that
        doesn't select it; it is not registered.
    """

    UNEXPOSED_WORKLOAD = "UNEXPOSED_WORKLOAD"
    TRANSIENT_WORKLOAD = "TRANSIENT_WORKLOAD"
    UNREAD_CONTROLLER = "UNREAD_CONTROLLER"
    SELECTORLESS_SERVICE = "SELECTORLESS_SERVICE"
    SERVICE_SELECTS_NO_WORKLOAD = "SERVICE_SELECTS_NO_WORKLOAD"
    EXTERNAL_NAME_SERVICE = "EXTERNAL_NAME_SERVICE"
    NAME_COLLISION = "NAME_COLLISION"


class ShapeFinding(BaseModel):
    """One namespace object outside the supported shape.

    ``object`` is ``<kind>/<name>`` (``service/x``, ``cronjob/y``);
    ``service_id`` is the FQURI of the service it concerns, when it has one.
    """

    namespace: str = Field(min_length=1)
    object: str = Field(min_length=1)
    kind: ShapeFindingKind
    detail: str = Field(min_length=1)
    service_id: Optional[str] = None


_DEFAULT_HTTPS_PORT: Final[int] = 443


def normalize_api_server(url: str) -> str:
    """Return a Kubernetes API server URL as ``https://<host>:<port>``.

    The host is lower-cased and the port made explicit, so the same server
    written two ways compares equal. Paths, queries and credentials are not
    allowed: the value identifies a cluster, nothing else.

    Raises:
        ValueError: if ``url`` is not an absolute https URL with a host.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port or _DEFAULT_HTTPS_PORT
    except ValueError as error:
        raise ValueError(f"invalid API server URL {url!r}: {error}") from error
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError(f"API server must be an https URL with a host: {url!r}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"API server URL must not carry credentials or a query: {url!r}")
    if parts.path not in ("", "/"):
        raise ValueError(f"API server URL must not have a path: {url!r}")
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"https://{host}:{port}"


# Telemetry attribute names (e.g. k8s.cluster.name) and values (e.g. an AKS
# node resource group) are interpolated into telemetry queries, so both are
# restricted to characters that cannot end a quoted string.
TELEMETRY_ATTRIBUTE_PATTERN: Final[str] = r"^[A-Za-z0-9_.-]{1,128}$"
TELEMETRY_VALUE_PATTERN: Final[str] = r"^[A-Za-z0-9_.-]{1,253}$"
DEFAULT_CLUSTER_ATTRIBUTE: Final[str] = "k8s.cluster.name"


class TelemetryClusterFilter(BaseModel):
    """Restricts telemetry to rows emitted from one cluster.

    Rows are kept only when their ``attribute`` (a resource attribute such
    as ``k8s.cluster.name``) equals ``value``. Rows without the attribute
    are dropped, so unscoped telemetry is never counted as this cluster's.
    """

    model_config = {"extra": "forbid", "frozen": True}

    attribute: str = Field(
        default=DEFAULT_CLUSTER_ATTRIBUTE, pattern=TELEMETRY_ATTRIBUTE_PATTERN
    )
    value: str = Field(pattern=TELEMETRY_VALUE_PATTERN)


# An Application Insights component's Azure resource ID as Log Analytics
# writes it in ``_ResourceId`` (lower-cased). IDs are interpolated into
# telemetry queries, so only characters that cannot end a quoted string.
APP_COMPONENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^/subscriptions/[0-9a-f-]{36}/resourcegroups/[a-z0-9._()-]{1,90}"
    r"/providers/microsoft\.insights/components/[a-z0-9._()-]{1,260}$"
)


# A Log Analytics workspace ID (customerId). One query may read up to 100
# workspaces (the primary and 99 more).
WORKSPACE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
MAX_ADDITIONAL_WORKSPACES: Final[int] = 99


def normalize_workspace_ids(ids: Iterable[str]) -> List[str]:
    """Return Log Analytics workspace IDs lower-cased, sorted, unique.

    Raises:
        ValueError: if an ID is not a workspace ID, or there are too many.
    """
    normalized = {raw.strip().lower() for raw in ids}
    for workspace_id in normalized:
        if not WORKSPACE_ID_PATTERN.match(workspace_id):
            raise ValueError(f"not a Log Analytics workspace ID (a GUID): {workspace_id!r}")
    if len(normalized) > MAX_ADDITIONAL_WORKSPACES:
        raise ValueError(f"at most {MAX_ADDITIONAL_WORKSPACES} additional workspaces")
    return sorted(normalized)


def normalize_component_ids(ids: Iterable[str]) -> List[str]:
    """Return Application Insights component resource IDs lower-cased, sorted, unique.

    Raises:
        ValueError: if an ID is not a component resource ID.
    """
    normalized = set()
    for raw in ids:
        component_id = raw.strip().rstrip("/").lower()
        if not APP_COMPONENT_ID_PATTERN.match(component_id):
            raise ValueError(
                "not an Application Insights component resource ID "
                f"(/subscriptions/<id>/resourceGroups/<rg>/providers/"
                f"microsoft.insights/components/<name>): {raw!r}"
            )
        normalized.add(component_id)
    return sorted(normalized)


class TelemetryScope(BaseModel):
    """Which telemetry rows count as evidence for the policy's cluster.

    ``cluster`` keeps rows whose resource attribute equals a value (OpenTelemetry
    with Kubernetes attributes, Beyla); ``components`` keeps rows written by
    one of these Application Insights components (``_ResourceId``), the only
    scope classic SDK and Azure Monitor Distro rows can prove, as they carry
    no cluster attribute. With both, a row must satisfy both. At least one is set.
    """

    model_config = {"extra": "forbid", "frozen": True}

    cluster: TelemetryClusterFilter | None = None
    components: List[str] = Field(default_factory=list)

    @field_validator("components")
    @classmethod
    def _normalize_components(cls, components: List[str]) -> List[str]:
        return normalize_component_ids(components)

    @model_validator(mode="after")
    def _scopes_something(self) -> Self:
        if self.cluster is None and not self.components:
            raise ValueError("a telemetry scope needs a cluster filter or components")
        return self

    def describe(self) -> str:
        """Human-readable form, e.g. ``k8s.cluster.name == 'prod' and component rc-ai``."""
        parts = []
        if self.cluster is not None:
            parts.append(f"{self.cluster.attribute} == {self.cluster.value!r}")
        if self.components:
            names = ", ".join(component.rsplit("/", 1)[-1] for component in self.components)
            noun = "component" if len(self.components) == 1 else "components"
            parts.append(f"Application Insights {noun} {names}")
        return " and ".join(parts)


def telemetry_scope_of(
    cluster: TelemetryClusterFilter | None, components: List[str]
) -> TelemetryScope | None:
    """The scope a cluster filter and component list make up; None when neither is set."""
    if cluster is None and not components:
        return None
    return TelemetryScope(cluster=cluster, components=components)


class ScanContext(BaseModel):
    """What one discovery run scanned, recorded in its baseline snapshot.

    ``environment`` and ``cluster`` are the deployment target the operator
    onboarded (never ``unknown``); every service FQURI in the snapshot
    carries them. ``namespaces`` are the Kubernetes namespaces scanned,
    ``telemetry_workspace_id`` the telemetry workspace queried (blank when
    none), and ``scanned_revisions`` one entry per repository read.
    ``kubernetes_api_server`` is the API server the manifest scan actually
    read (normalized, blank when no namespace was scanned) and
    ``telemetry_cluster`` the cluster filter the telemetry query applied
    (None when unfiltered), ``telemetry_components`` the Application
    Insights components it was restricted to (see ``TelemetryScope``). Both
    are checked against the project policy, so the ``cluster`` label is
    backed by what was really read. ``telemetry_additional_workspaces`` are
    the workspaces read together with ``telemetry_workspace_id``.
    """

    environment: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
    cluster: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
    namespaces: List[str] = Field(default_factory=list)
    telemetry_workspace_id: str = ""
    scanned_revisions: List[ScannedRevision] = Field(default_factory=list)
    kubernetes_api_server: str = ""
    telemetry_cluster: Optional[TelemetryClusterFilter] = None
    telemetry_components: List[str] = Field(default_factory=list)
    telemetry_additional_workspaces: List[str] = Field(default_factory=list)

    @field_validator("telemetry_additional_workspaces")
    @classmethod
    def _normalize_workspaces(cls, workspaces: List[str]) -> List[str]:
        return normalize_workspace_ids(workspaces)

    @field_validator("kubernetes_api_server")
    @classmethod
    def _normalize_api_server(cls, value: str) -> str:
        return normalize_api_server(value) if value else ""

    @field_validator("telemetry_components")
    @classmethod
    def _normalize_components(cls, components: List[str]) -> List[str]:
        return normalize_component_ids(components)

    @property
    def telemetry_scope(self) -> TelemetryScope | None:
        """The telemetry scope the query applied, None when unscoped."""
        return telemetry_scope_of(self.telemetry_cluster, self.telemetry_components)

    @model_validator(mode="after")
    def _check_scope(self) -> Self:
        if UNKNOWN_LABEL in (self.environment, self.cluster):
            raise ValueError("environment and cluster must be known")
        repositories = [revision.repository for revision in self.scanned_revisions]
        if len(set(repositories)) != len(repositories):
            raise ValueError("scanned_revisions must list each repository once")
        return self

    def revision_for(self, repository: str) -> ScannedRevision | None:
        """Return the revision scanned for ``repository``, if any."""
        return next(
            (r for r in self.scanned_revisions if r.repository == repository), None
        )


class TargetDetailKind(str, Enum):
    """What a ``TargetDetail`` names behind an edge's target host.

    APP_COMPONENT: the Application Insights component (``cid-v1:<appId>``)
        the callee reports to, as the classic SDK records for calls into
        another instrumented service.
    RESOURCE: a named resource the host serves: a database, a queue, a
        topic, an event hub or a container.
    ADDRESS: the address (host[:port]) the caller used, kept on an edge whose
        target was identified by request join rather than by that address.
    """

    APP_COMPONENT = "app_component"
    RESOURCE = "resource"
    ADDRESS = "address"


class EdgeResolution(str, Enum):
    """How a telemetry edge's target was identified.

    TARGET: from the address the caller recorded (host, URL, data source).
    REQUEST_JOIN: from the callee's own telemetry: the request (or message
        processing) whose parent span is the caller's dependency span, in
        the same operation. It names the callee's role even when the address
        is a gateway, an IP, a broker or another component's ID.
    """

    TARGET = "target"
    REQUEST_JOIN = "request_join"


class TargetDetail(BaseModel):
    """One named thing behind a telemetry target's host (see ``TargetDetailKind``)."""

    kind: TargetDetailKind
    name: str = Field(min_length=1)

    model_config = {"frozen": True}


class DependencyEdge(BaseModel):
    """A directed, evidence-backed relationship between two service nodes.

    ``provenance`` names where the evidence came from (for example
    ``azure_devops``, ``azure_monitor`` or ``mock_fixture``) and
    ``environment`` the deployment environment it was observed in. Merged
    edges carry comma-separated labels. Both default to ``unknown`` so
    snapshots written before they existed still load. Mock fixture evidence
    is demo data, never real evidence, so it must carry zero confidence.

    ``dependency_type`` is the telemetry's dependency type (``HTTP``,
    ``Queue Message | rabbitmq``...), when known. ``direction_uncertain``
    marks a telemetry edge whose target port is in the ephemeral range: it
    may be a call to a server listening high, or a reply reported in
    reverse. A merged edge is uncertain only if all its evidence is.

    ``protocol`` is the wire protocol the source's address for the target
    names (``http``, ``grpc``, ``amqp``, ``postgres``... see
    ``telemetry_capability.wire_protocol``), ``unknown`` when the scheme
    doesn't say, None when no address scheme was seen (telemetry edges,
    bare host names).

    ``evidence_files`` are the source service's repository-relative files
    whose code or configuration references the target (every file, not
    just the first), so a reviewer can check the evidence and a review
    answer can be anchored to it. Empty when no file is the evidence
    (telemetry, Kubernetes environment); None in snapshots written before
    it was recorded.

    ``target_details`` are the named things telemetry reported behind the
    target host (the database, queue or Application Insights component
    of ``server | db``, ``sb://ns/ | queue`` or ``host | cid-v1:<appId>``),
    sorted and unique. None when no telemetry row named any.

    ``resolution`` says how telemetry identified the target (see
    ``EdgeResolution``), sorted and unique; None for edges from other
    sources and snapshots written before it was recorded.
    """

    source_service_id: str
    target_service_id: str
    discovery_methods: List[DiscoveryMethod]
    confidence_score: float = Field(ge=0.0, le=1.0)
    last_observed_at: datetime
    provenance: str = Field(default=UNKNOWN_LABEL, min_length=1)
    environment: str = Field(default=UNKNOWN_LABEL, min_length=1)
    dependency_type: str | None = None
    direction_uncertain: bool = False
    protocol: str | None = None
    evidence_files: List[str] | None = None
    target_details: List[TargetDetail] | None = None
    resolution: List[EdgeResolution] | None = None

    @field_validator("resolution")
    @classmethod
    def _sorted_unique_resolution(
        cls, methods: List[EdgeResolution] | None
    ) -> List[EdgeResolution] | None:
        return merge_resolution(methods, None)

    @field_validator("target_details")
    @classmethod
    def _sorted_unique_details(
        cls, details: List[TargetDetail] | None
    ) -> List[TargetDetail] | None:
        return merge_target_details(details, None)

    @field_validator("evidence_files")
    @classmethod
    def _relative_sorted_files(cls, files: List[str] | None) -> List[str] | None:
        if files is None:
            return None
        return sorted({path.lstrip("/") for path in files if path.strip("/")})

    @model_validator(mode="after")
    def _mock_fixtures_carry_no_confidence(self) -> Self:
        if self.provenance == MOCK_FIXTURE_PROVENANCE and self.confidence_score != 0.0:
            raise ValueError("mock_fixture edges must have a confidence_score of 0.0")
        return self

    @property
    def is_mock_fixture(self) -> bool:
        """Whether any of this edge's evidence comes from a mock fixture."""
        return MOCK_FIXTURE_PROVENANCE in self.provenance.split(LABEL_SEPARATOR)


def merge_target_details(
    first: List[TargetDetail] | None, second: List[TargetDetail] | None
) -> List[TargetDetail] | None:
    """Union two edges' target details, sorted; None when neither recorded any.

    Edges updated with ``model_copy`` skip validation, so every writer of
    ``target_details`` goes through here to keep them sorted and unique.
    """
    merged = {*(first or []), *(second or [])}
    if not merged:
        return None
    return sorted(merged, key=lambda detail: (detail.kind.value, detail.name))


def merge_resolution(
    first: List[EdgeResolution] | None, second: List[EdgeResolution] | None
) -> List[EdgeResolution] | None:
    """Union two edges' resolution methods, sorted; None when neither recorded any."""
    merged = {*(first or []), *(second or [])}
    return sorted(merged, key=lambda method: method.value) if merged else None


def merge_evidence_files(
    first: List[str] | None, second: List[str] | None
) -> List[str] | None:
    """Union two edges' evidence files; None only when neither recorded any."""
    if first is None and second is None:
        return None
    return sorted({*(first or []), *(second or [])})


class KnowledgeGraph(BaseModel):
    """Shape of one persisted discovery baseline snapshot.

    Every edge endpoint is either a registered service's FQURI or the ID of
    an ``UnresolvedEntity``; snapshots that break this (including those
    written with the older ``<namespace>/<name>`` IDs) fail validation.
    ``discovery`` is None for graphs written before discovery outcomes were
    recorded; such a graph cannot prove its discovery was complete.
    ``scan`` is None for graphs written before the scanned environment,
    cluster and revisions were recorded. When present, every service FQURI
    must carry its environment and cluster, and every service found in a
    repository must name a repository with a scanned revision.
    ``service_environments`` are the container environments the manifest
    scan read (None in snapshots written before they were recorded); a
    pull request's head references are resolved through them.
    ``static_coverage`` records how completely each service directory was
    scanned, ``pipeline_variables`` what each repository's pipelines
    define, ``kubernetes_shape`` the namespace objects outside the
    supported shape, ``static_config`` the configuration defaults each
    service's source declares, and ``static_defaults_used`` the
    dependencies resolved from those defaults; each is None in snapshots
    written before it was.
    """

    services: List[ServiceNode]
    edges: List[DependencyEdge]
    discovery: Optional[List[DiscoveryStageReport]] = None
    unresolved_entities: List[UnresolvedEntity] = Field(default_factory=list)
    scan: Optional[ScanContext] = None
    service_environments: Optional[List[ServiceEnvironment]] = None
    static_coverage: Optional[List[RepositoryCoverage]] = None
    pipeline_variables: Optional[List[PipelineVariables]] = None
    kubernetes_shape: Optional[List[ShapeFinding]] = None
    static_config: Optional[List[ServiceStaticConfig]] = None
    static_defaults_used: Optional[List[StaticDefaultUse]] = None

    @model_validator(mode="after")
    def _services_match_scan(self) -> Self:
        if self.scan is None:
            return self
        target = (self.scan.environment, self.scan.cluster)
        outside = sorted(
            service.service_id
            for service in self.services
            if (service.identity.environment, service.identity.cluster) != target
        )
        if outside:
            raise ValueError(
                f"services are not in {target[0]}/{target[1]}: {outside}"
            )
        unscanned = sorted(
            {
                repository
                for service in self.services
                if (repository := service.metadata.get(REPOSITORY_METADATA_KEY))
                and self.scan.revision_for(repository) is None
            }
        )
        if unscanned:
            raise ValueError(f"repositories have no scanned revision: {unscanned}")
        return self

    @model_validator(mode="after")
    def _edges_join_to_nodes(self) -> Self:
        service_ids = [service.service_id for service in self.services]
        entity_ids = [entity.entity_id for entity in self.unresolved_entities]
        if len(set(service_ids)) != len(service_ids):
            raise ValueError("service IDs must be unique")
        if len(set(entity_ids)) != len(entity_ids):
            raise ValueError("unresolved entity IDs must be unique")
        nodes = {*service_ids, *entity_ids}
        dangling = sorted(
            {
                endpoint
                for edge in self.edges
                for endpoint in (edge.source_service_id, edge.target_service_id)
                if endpoint not in nodes
            }
        )
        if dangling:
            raise ValueError(
                "edge endpoints must be a registered service or an unresolved "
                f"entity: {dangling}"
            )
        return self

    @property
    def uses_mock_data(self) -> bool:
        """Whether any edge in this baseline relies on mock fixture evidence."""
        return any(edge.is_mock_fixture for edge in self.edges)


def ensure_complete_baseline(graph: KnowledgeGraph) -> None:
    """Raise unless ``graph`` records a complete discovery run.

    Raises:
        DiscoveryIncompleteError: if discovery outcomes are missing, or any
            stage FAILED, was PARTIAL, or did not report.
    """
    if graph.discovery is None:
        raise DiscoveryIncompleteError(
            ["knowledge graph does not record discovery outcomes"]
        )
    problems = baseline_problems(graph.discovery)
    if problems:
        raise DiscoveryIncompleteError(problems)


class ContractChange(str, Enum):
    """How a pull request changes the target services' contracts.

    ADDITIVE: compatible change (new optional fields or endpoints; old
        inputs and messages still accepted). Providers deploy before
        consumers.
    BREAKING: tightening or breaking change (a field becomes required, is
        removed or renamed, validation becomes stricter, or a new message
        schema version is published). Consumers deploy before providers.
    """

    ADDITIVE = "additive"
    BREAKING = "breaking"


class PullRequestPayload(BaseModel):
    """A pull request that triggers a release impact assessment.

    ``target_service`` is a FQURI; segments left ``unknown`` act as
    wildcards when the target is resolved against the baseline inventory.
    ``change_type`` is declared by the caller (for example from a PR label)
    and decides the deterministic deployment order. ``repository``,
    ``base_sha``, ``head_sha`` and ``changed_files`` identify the exact
    revisions assessed; they are blank when the caller did not supply them.
    """

    pr_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    target_service: str = Field(min_length=1)
    change_type: ContractChange
    source_branch: str = ""
    target_branch: str = "main"
    description: str = ""
    diff: str = Field(min_length=1)
    repository: str = ""
    base_sha: str = ""
    head_sha: str = ""
    changed_files: List[str] = Field(default_factory=list)

    @field_validator("target_service")
    @classmethod
    def _target_is_fquri(cls, value: str) -> str:
        ServiceIdentity.parse(value)
        return value


class RuntimeEvidenceState(str, Enum):
    """What runtime evidence backs one assessment.

    LIVE: a telemetry workspace was queried for this assessment, the query
        completed, its newest row is fresh, and every service in the release
        radius was observed (or is an allowlisted silent service).
    LIVE_EMPTY: the live query completed but returned no usable rows.
    LIVE_STALE: the newest live row is older than the policy allows.
    LIVE_UNCOVERED: some service in the release radius was not observed.
    LIVE_UNOBSERVABLE: every radius service was observed or allowed, except
        services the configured telemetry can't see: each of their
        dependencies uses a protocol outside its capability (or an unknown
        one), so their absence proves nothing.
    LIVE_INCOMPLETE: the live query FAILED or was PARTIAL.
    MOCK: the live query was answered from the demo fixture.
    BASELINE: no workspace was queried; the baseline snapshot's
        RUNTIME_TELEMETRY stage succeeded, so its runtime edges are used.
    NONE: there is no runtime evidence (no workspace was queried and the
        baseline has no successful RUNTIME_TELEMETRY stage, or no baseline).
    """

    LIVE = "live"
    LIVE_EMPTY = "live_empty"
    LIVE_STALE = "live_stale"
    LIVE_UNCOVERED = "live_uncovered"
    LIVE_UNOBSERVABLE = "live_unobservable"
    LIVE_INCOMPLETE = "live_incomplete"
    MOCK = "mock"
    BASELINE = "baseline"
    NONE = "none"


class ObservedEdge(BaseModel):
    """A dependency live telemetry observed, resolved against the baseline.

    Endpoints are registered service FQURIs or unresolved entity IDs.
    ``direction_uncertain`` is true when every sighting reported a target
    port in the ephemeral range (see ``DependencyEdge``).
    """

    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    last_observed_at: datetime
    direction_uncertain: bool = False


class UnresolvedReach(BaseModel):
    """An unresolved node reached by the radius traversal, and why it matters.

    ``reference`` and ``entity_kind`` come from the unresolved entity (blank
    and None for an endpoint missing from the inventory); ``via`` is the
    path from the release, in call direction.
    """

    node: str
    detail: str
    reference: str = ""
    entity_kind: Optional[UnresolvedEntityKind] = None
    via: List[str] = Field(default_factory=list)


class LiveCoverage(BaseModel):
    """How well one live telemetry query covered the release radius.

    ``edge_count`` counts resolved live edges after normalization and
    ``newest_observed_at`` is the latest of them (None when there are
    none). Coverage is judged per radius service from
    ``service_last_seen`` (when it last appeared as a source or target):
    ``observed_services`` were seen within the live window,
    ``silent_services`` weren't but are within an active silent-service
    allowance, and ``uncovered_services`` are neither.
    ``expired_exemptions`` names radius services whose silent-service entry
    has expired and was ignored.

    ``unscoped_sources`` (None when not checked) counts, per telemetry
    source name, the rows the cluster filter dropped because they lack the
    cluster attribute; ``unscoped_services`` maps uncovered radius services
    to the unscoped source names that look like them. Unscoped rows never
    count as coverage; they explain why a service looks silent.

    ``unobservable_services`` maps radius services not seen whose every
    dependency uses a protocol the telemetry can't see (or an unknown one)
    to those protocols; ``accepted_unobservable`` lists those an active
    ``exemptions.unobservable`` entry covers for each protocol. A silent
    allowance never applies to an unobservable service.
    ``telemetry_profiles`` maps radius services to the capability profiles
    (``telemetry_capability.TelemetryProfile``) their own telemetry shows;
    empty when the role profiles weren't read, and then one default profile
    judged every edge.

    ``sampled_sources`` maps telemetry sources whose rows were sampled to
    the largest number of calls one row stood for (``ItemCount``): with
    sampling, a service called rarely can have no row in the window while
    working, so an uncovered verdict says so.
    """

    edge_count: int = Field(ge=0)
    newest_observed_at: Optional[datetime] = None
    observed_services: List[str] = Field(default_factory=list)
    uncovered_services: List[str] = Field(default_factory=list)
    silent_services: List[str] = Field(default_factory=list)
    service_last_seen: Dict[str, datetime] = Field(default_factory=dict)
    expired_exemptions: List[str] = Field(default_factory=list)
    unscoped_sources: Optional[Dict[str, int]] = None
    unscoped_services: Dict[str, List[str]] = Field(default_factory=dict)
    unobservable_services: Dict[str, List[str]] = Field(default_factory=dict)
    accepted_unobservable: Dict[str, List[str]] = Field(default_factory=dict)
    telemetry_profiles: Dict[str, List[str]] = Field(default_factory=dict)
    sampled_sources: Dict[str, int] = Field(default_factory=dict)


class TelemetryRoleProfile(BaseModel):
    """What the telemetry holds for one role (service name) over the lookback.

    ``role`` is the row's ``service.name`` or ``AppRoleName`` ("" for rows
    without one) and ``namespace`` a Kubernetes namespace its rows carry
    ("" when none do). Row counts are rows stored; ``represented_calls`` is
    what they stand for (``sum(ItemCount)``), so ``max_item_count`` above 1
    means the role is sampled. ``stacks`` classify ``sdk_versions`` (see
    ``TelemetryStack``); ``components`` are Application Insights component
    names its rows came from. ``service_bus_activity_rows`` count rows the
    Azure Monitor Distro writes only with its experimental activity source
    on (``Microsoft.ServiceBus`` dependencies, ``ServiceBusProcessor.*``
    requests).
    """

    role: str
    namespace: str = ""
    request_rows: int = Field(ge=0)
    dependency_rows: int = Field(ge=0)
    represented_calls: int = Field(ge=0)
    max_item_count: int = Field(ge=0)
    rows_with_namespace: int = Field(ge=0)
    stacks: List[TelemetryStack] = Field(default_factory=list)
    sdk_versions: List[str] = Field(default_factory=list)
    components: List[str] = Field(default_factory=list)
    last_seen: Optional[datetime] = None
    service_bus_activity_rows: int = Field(default=0, ge=0)

    @property
    def rows(self) -> int:
        """Rows stored for the role."""
        return self.request_rows + self.dependency_rows


class ObservabilityGapKind(str, Enum):
    """Why telemetry describes a service less well than it could.

    NO_ROLE_NAME: rows without a service name: their calls can't be
        attributed to any service.
    ROLE_NOT_IN_INVENTORY: a telemetry role that matches no inventory
        service (renamed, another application, or a name that differs from
        the Kubernetes one).
    SERVICE_WITHOUT_TELEMETRY: an inventory service with no rows in scope.
    NO_NAMESPACE: the service's rows carry no Kubernetes namespace, so it is
        matched by bare name.
    NO_REQUEST_TELEMETRY: the service writes dependencies but no requests,
        so calls to it can't be confirmed by request join, only by address.
    SAMPLED: the service's telemetry is sampled; rare calls may be missing.
    CONSUMER_NOT_TRACED: the service sends messages but no consumer's
        processing of them is traced, so who consumes them isn't observed.
    """

    NO_ROLE_NAME = "no_role_name"
    ROLE_NOT_IN_INVENTORY = "role_not_in_inventory"
    SERVICE_WITHOUT_TELEMETRY = "service_without_telemetry"
    NO_NAMESPACE = "no_namespace"
    NO_REQUEST_TELEMETRY = "no_request_telemetry"
    SAMPLED = "sampled"
    CONSUMER_NOT_TRACED = "consumer_not_traced"


class ObservabilityGap(BaseModel):
    """One way the telemetry falls short for one subject.

    ``subject`` is a service FQURI when the role resolved to one, otherwise
    the telemetry role (``<namespace>/<role>`` or the bare role; ``""`` for
    rows without a role name). ``stacks`` is the instrumentation seen.
    """

    kind: ObservabilityGapKind
    subject: str
    detail: str = Field(min_length=1)
    stacks: List[TelemetryStack] = Field(default_factory=list)


class TelemetryStatus(BaseModel):
    """Runtime evidence available for the services in a release radius.

    ``source`` is ``knowledge_graph`` when the evidence comes only from the
    RUNTIME_TELEMETRY edges persisted by discovery, ``live`` when a telemetry
    workspace was queried for this assessment, or ``mock`` when that query
    was answered from the demo fixture instead (its metrics are not real).
    ``runtime_evidence`` classifies that evidence (see
    ``RuntimeEvidenceState``); it defaults to NONE so an unclassified status
    never counts as runtime evidence. ``unbaselined_edges`` are live edges
    touching the release radius that the baseline does not have.

    ``runtime_observed_edges`` are the edges this assessment's live query
    observed in the radius (empty without a live query);
    ``baseline_runtime_edges`` the radius edges the baseline holds from
    telemetry. ``runtime_metrics`` is keyed by service FQURI.
    ``coverage`` and ``dropped_records`` describe the live query;
    ``observability_gaps`` how the telemetry falls short for radius services
    (None when not probed).
    """

    source: Literal["knowledge_graph", "live", "mock"]
    runtime_evidence: RuntimeEvidenceState = RuntimeEvidenceState.NONE
    runtime_observed_edges: List[str] = Field(default_factory=list)
    baseline_runtime_edges: List[str] = Field(default_factory=list)
    runtime_metrics: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    unbaselined_edges: List[ObservedEdge] = Field(default_factory=list)
    coverage: Optional[LiveCoverage] = None
    dropped_records: Dict[str, int] = Field(default_factory=dict)
    observability_gaps: Optional[List[ObservabilityGap]] = None


class RiskAssessment(BaseModel):
    """LLM-produced release risk verdict for a pull request.

    The deployment sequence is computed deterministically before the LLM is
    called, so the model only scores, approves and justifies it.
    """

    risk_score: float = Field(ge=0.0, le=1.0)
    approved: bool
    justification: str = Field(min_length=1)


class ReasonCode(str, Enum):
    """Deterministic reason a release assessment is not approved.

    BASELINE_UNAVAILABLE: no usable baseline snapshot (none exists, or the
        pinned snapshot cannot be read).
    DISCOVERY_GAP: dependency evidence is incomplete (an unresolved target
        or dependency in the radius, or a failed or partial telemetry query).
    UNMAPPED_FILES: a changed file is not owned by any registered service.
    DEMO_EVIDENCE: the evidence includes mock fixture data.
    DEPENDENCY_CYCLE: no deployment order satisfies every dependency edge.
    COMPATIBILITY_UNVERIFIED: the deployment order follows the declared
        change type; no contract compatibility evidence proves it safe (see
        ``ContractCheck`` for when it does).
    CONTRACT_BREAKING: a changed contract file (``.proto``) breaks
        compatibility: services built against the old and the new version
        can't talk during a rolling deployment. A fully evidenced risk, so
        the assessment stays actionable; a person decides.
    MODEL_REJECTED: the advisory model assessment did not approve.
    POLICY_MISSING: the project has no release policy file.
    POLICY_INVALID: the project's policy file is malformed or names another
        project.
    BASELINE_STALE: the baseline snapshot is older than the policy allows.
    REQUIRED_SOURCE_MISSING: an evidence source the policy requires did not
        succeed in the baseline, or the runtime evidence is weaker than the
        policy requires.
    BASELINE_DRIFT: live telemetry observed a dependency touching the
        release radius that the baseline does not have; re-run discovery.
    BASELINE_SCOPE_MISMATCH: the baseline does not record the environment
        and cluster it scanned, or they differ from the project policy's.
    REVISION_UNVERIFIED: the pull request's revisions couldn't be verified
        with source control: a commit is missing, the base isn't an ancestor
        of the head, the changed files differ from what the commits change,
        or the head couldn't be scanned.
    BASELINE_REVISION_MISMATCH: the commits between the baseline's scanned
        revision and the pull request's base change services in the radius,
        or the two revisions can't be related; re-run discovery or rebase.
    STATIC_COVERAGE_INCOMPLETE: a service directory has source files the
        scanner couldn't read (an unsupported language, a read failure), at
        the baseline or the pull request's head, or the baseline predates
        coverage accounting; a dependency they declare is invisible.
    RUNTIME_UNOBSERVABLE: live evidence is required, but a radius service
        talks only protocols the configured telemetry can't see, so its
        absence proves nothing; accepted per protocol only through
        ``exemptions.unobservable``.
    IDENTITY_UNLINKED: a service in the radius exists in source code but
        matches no running service (by name, or by name up to case and
        separators), so its runtime calls and callers are unknown.
    REVIEW_UNAVAILABLE: the project's dependency review answers couldn't be
        read, so dependencies a coordinator declared may be missing from
        the deployment order.
    ADVISORY_DISABLED: the evidence is complete, but the project policy
        keeps its data from the external model (``external_ai: false``), so
        no advisory risk review was made and nothing is approved
        automatically; a person decides.

    Every code except MODEL_REJECTED, ADVISORY_DISABLED and
    CONTRACT_BREAKING makes the
    assessment non-actionable: the evidence is insufficient to rely on the
    verdict, which is different from a fully evidenced verdict that the
    release is too risky or that a person must judge.
    """

    BASELINE_UNAVAILABLE = "BASELINE_UNAVAILABLE"
    DISCOVERY_GAP = "DISCOVERY_GAP"
    UNMAPPED_FILES = "UNMAPPED_FILES"
    DEMO_EVIDENCE = "DEMO_EVIDENCE"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    COMPATIBILITY_UNVERIFIED = "COMPATIBILITY_UNVERIFIED"
    CONTRACT_BREAKING = "CONTRACT_BREAKING"
    MODEL_REJECTED = "MODEL_REJECTED"
    POLICY_MISSING = "POLICY_MISSING"
    POLICY_INVALID = "POLICY_INVALID"
    BASELINE_STALE = "BASELINE_STALE"
    REQUIRED_SOURCE_MISSING = "REQUIRED_SOURCE_MISSING"
    BASELINE_DRIFT = "BASELINE_DRIFT"
    BASELINE_SCOPE_MISMATCH = "BASELINE_SCOPE_MISMATCH"
    REVISION_UNVERIFIED = "REVISION_UNVERIFIED"
    BASELINE_REVISION_MISMATCH = "BASELINE_REVISION_MISMATCH"
    STATIC_COVERAGE_INCOMPLETE = "STATIC_COVERAGE_INCOMPLETE"
    IDENTITY_UNLINKED = "IDENTITY_UNLINKED"
    RUNTIME_UNOBSERVABLE = "RUNTIME_UNOBSERVABLE"
    REVIEW_UNAVAILABLE = "REVIEW_UNAVAILABLE"
    ADVISORY_DISABLED = "ADVISORY_DISABLED"

    @property
    def limits_actionability(self) -> bool:
        """Whether this reason makes the assessment ineligible for operational use."""
        return self not in (
            ReasonCode.MODEL_REJECTED,
            ReasonCode.ADVISORY_DISABLED,
            ReasonCode.CONTRACT_BREAKING,
        )


class IssueSubjectKind(str, Enum):
    """What an evidence issue is about."""

    SERVICE = "service"
    ENTITY = "entity"
    FILE = "file"


class IssueSubject(BaseModel, frozen=True):
    """One thing an evidence issue concerns: a service (its FQURI, or its
    name where only the name is known), an unresolved entity ID, or a
    repository file (``<repository>:<path>``)."""

    kind: IssueSubjectKind
    id: str = Field(min_length=1)


class EvidenceIssue(BaseModel):
    """One reason the evidence behind an assessment is insufficient.

    ``detail`` is the sentence a person reads. The other fields let a
    briefing group issues by cause without parsing it: ``subjects`` are
    what the issue concerns, ``entity_kind`` the kind of an unresolved
    entity, ``via`` the dependency path (call direction) that reached it,
    and ``root_key`` a deterministic key shared by issues with one cause
    (the same variable unset in several services, the same files not
    understood in every consumer of a library). It defaults to the code.
    """

    code: ReasonCode
    detail: str = Field(min_length=1)
    subjects: List[IssueSubject] = Field(default_factory=list)
    entity_kind: Optional[UnresolvedEntityKind] = None
    root_key: str = ""
    via: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_root_key(self) -> Self:
        if not self.root_key:
            self.root_key = self.code.value
        return self


class Exemption(BaseModel):
    """A changed file or unresolved dependency a project policy exempted.

    ``subject`` is the file path or unresolved entity ID; ``rule`` is the
    policy pattern or entity ID that matched it, and ``reason`` the policy's
    justification for the exemption.
    """

    subject: str = Field(min_length=1)
    rule: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class DependencyChange(BaseModel):
    """A code dependency a pull request's head adds or no longer declares."""

    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)


class TestScopeTier(BaseModel):
    """One tier of the suggested test scope for a release (see ``coordinator.test_scope``)."""

    tier: int = Field(ge=1)
    title: str = Field(min_length=1)
    why: str = Field(min_length=1)
    services: List[str] = Field(min_length=1)


class RegulatedImpact(BaseModel):
    """How a pull request touches one service the policy marks as regulated.

    ``reasons`` say what touches it: the PR changes it, or adds or removes a
    dependency to or from it. It flags a possible significant change for a
    person's regulatory decision; it never changes the verdict.
    """

    service: str = Field(min_length=1)
    classification: str = ""
    reasons: List[str] = Field(min_length=1)


class ReviewNote(BaseModel):
    """One dependency review answer an assessment used or set aside.

    ``subject`` is the answer's subject key, ``detail`` what it did here
    (or why it no longer counts).
    """

    answer_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    answered_at: datetime
    reason: Optional[str] = None
    detail: str = ""


class AssessmentReview(BaseModel):
    """How the project's dependency review shaped one assessment.

    ``applied`` are the answers in force that changed the map or closed a
    question; ``accepted_issues`` the issues those answers turned into
    context; ``needs_rereview`` answers that no longer count (the gap or
    edge they covered is back in the evidence). In the release radius,
    ``confirmed`` / ``declared`` / ``unreviewed`` are the dependencies a
    coordinator agreed with, declared, or never reviewed; ``removed``
    those a coordinator removed. ``unapplied`` are answers that couldn't
    be applied, with why.
    """

    applied: List[ReviewNote] = Field(default_factory=list)
    accepted_issues: List[EvidenceIssue] = Field(default_factory=list)
    needs_rereview: List[ReviewNote] = Field(default_factory=list)
    confirmed: List[DependencyChange] = Field(default_factory=list)
    declared: List[DependencyChange] = Field(default_factory=list)
    removed: List[DependencyChange] = Field(default_factory=list)
    unreviewed: int = 0
    unapplied: List[str] = Field(default_factory=list)


class ContractScope(str, Enum):
    """When contract evidence may verify a release's compatibility (policy).

    STRICT: only when the pull request changes nothing but contract files.
    INTERFACES: also when it changes code, provided every caller of each
        changed service reaches it through a contract both sides compile
        (``ContractLink``), and the changed contracts are compatible.
    """

    STRICT = "strict"
    INTERFACES = "interfaces"


class ContractCheckStatus(str, Enum):
    """The outcome of the contract check, in the order it is decided.

    UNREADABLE: a changed contract file couldn't be read or understood.
    BREAKING: a changed contract breaks compatibility (CONTRACT_BREAKING).
    NO_CONTRACT: no contract evidence covers the change (no contract file
        changed, or, under INTERFACES, a caller isn't covered).
    CODE_CHANGED: under STRICT, the pull request also changes other files.
    COMPATIBLE: the evidence proves the change compatible.
    """

    UNREADABLE = "UNREADABLE"
    BREAKING = "BREAKING"
    NO_CONTRACT = "NO_CONTRACT"
    CODE_CHANGED = "CODE_CHANGED"
    COMPATIBLE = "COMPATIBLE"


class ContractCheck(BaseModel):
    """Contract compatibility evidence for one pull request.

    ``contract_files`` are the changed ``.proto`` files, ``other_files`` the
    rest of the changed files. ``findings`` are every difference between
    their base and head versions, ``unreadable`` the files that couldn't
    be read or parsed (with why). ``consumers`` maps each changed contract
    file to the services whose builds compile it as a client.
    ``uncovered_callers`` are, under INTERFACES, callers of a changed
    service that reach it through no shared contract. ``detail`` is the
    sentence a person reads.
    """

    scope: ContractScope = ContractScope.STRICT
    status: ContractCheckStatus
    contract_files: List[str] = Field(default_factory=list)
    other_files: List[str] = Field(default_factory=list)
    findings: List[ContractFinding] = Field(default_factory=list)
    unreadable: Dict[str, str] = Field(default_factory=dict)
    consumers: Dict[str, List[str]] = Field(default_factory=dict)
    uncovered_callers: List[str] = Field(default_factory=list)
    detail: str = Field(min_length=1)

    @property
    def compatible(self) -> bool:
        return self.status is ContractCheckStatus.COMPATIBLE

    @property
    def breaking_findings(self) -> List[ContractFinding]:
        return [finding for finding in self.findings if finding.breaking]


class AssessmentEvidence(BaseModel):
    """The exact inputs one release assessment was computed from.

    ``baseline_snapshot_id`` is None when no snapshot could be loaded, and
    ``baseline_created_at`` is when that snapshot was written.
    ``changed_files`` is the full list the caller supplied, including the
    ``unmapped_files`` no registered service owns and the
    ``exempted_files`` the project policy excludes from that check.
    ``exempted_dependencies`` are unresolved dependencies in the radius the
    policy exempted. ``demo_mode`` is true when any evidence came from mock
    fixture data. ``policy_version`` and ``policy_sha256`` identify the
    project policy applied (None when it could not be loaded).
    ``baseline_environment``, ``baseline_cluster`` and ``scanned_revisions``
    are what the baseline snapshot scanned (None and empty for snapshots
    that predate scan recording). ``unbaselined_edges`` are live edges in
    the release radius missing from the baseline. ``head_scanned`` is true
    when the pull request's revisions were verified and its changed
    services scanned at the head; ``dependencies_added`` and
    ``dependencies_removed`` are the code dependencies the head changed.
    ``exempted_unscanned_files`` are unscanned source files the policy
    accepts; ``shape_findings`` are out-of-shape Kubernetes objects that
    concern a service in the release radius. ``review`` is how the
    project's dependency review shaped the assessment (None when the
    project has no review answers).
    """

    pr_id: str = Field(min_length=1)
    repository: str = ""
    base_sha: str = ""
    head_sha: str = ""
    baseline_snapshot_id: Optional[str] = None
    baseline_created_at: Optional[datetime] = None
    changed_files: List[str] = Field(default_factory=list)
    unmapped_files: List[str] = Field(default_factory=list)
    exempted_files: List[Exemption] = Field(default_factory=list)
    exempted_dependencies: List[Exemption] = Field(default_factory=list)
    demo_mode: bool = False
    runtime_evidence: RuntimeEvidenceState = RuntimeEvidenceState.NONE
    policy_version: Optional[int] = None
    policy_sha256: Optional[str] = None
    baseline_environment: Optional[str] = None
    baseline_cluster: Optional[str] = None
    scanned_revisions: List[ScannedRevision] = Field(default_factory=list)
    unbaselined_edges: List[ObservedEdge] = Field(default_factory=list)
    # How the live query covered the radius (None without a real live query).
    live_coverage: Optional["LiveCoverage"] = None
    # How the telemetry falls short for radius services (None when not probed).
    observability_gaps: Optional[List[ObservabilityGap]] = None
    head_scanned: bool = False
    dependencies_added: List[DependencyChange] = Field(default_factory=list)
    dependencies_removed: List[DependencyChange] = Field(default_factory=list)
    exempted_unscanned_files: List[Exemption] = Field(default_factory=list)
    shape_findings: List[ShapeFinding] = Field(default_factory=list)
    review: Optional[AssessmentReview] = None
    # Regulated services this PR touches (see RegulatedImpact).
    regulated_impact: List[RegulatedImpact] = Field(default_factory=list)
    # Suggested test scope by hop distance from the changed services.
    test_scope: List[TestScopeTier] = Field(default_factory=list)
    # What was redacted, per kind, from what the advisory model was sent.
    advisory_redactions: Dict[str, int] = Field(default_factory=dict)
    # Contract compatibility evidence (None for records made before it existed).
    contract_check: Optional[ContractCheck] = None


class ReleaseAssessment(BaseModel):
    """Machine-readable release verdict returned to the control plane.

    ``approved`` is the final policy decision, made in code: it is true only
    when the advisory model approved and ``reason_codes`` is empty.
    ``actionable`` says whether the result is eligible for operational use;
    it is false whenever a reason code limits actionability (see
    ``ReasonCode``). ``risk_score`` is None when the release was not
    assessed because the evidence is insufficient; missing evidence is not
    scored as high risk. ``advisory_assessment`` is the model's opinion, if
    it was consulted. ``safe_order_established`` is true only when the
    deployment order is acyclic and its compatibility is verified.

    ``deployment_sequence`` orders the whole release subgraph;
    ``services_to_deploy`` is the part of it the pull request changes, in the
    same order. ``stale_consumers`` lists unchanged upstream consumers that a
    BREAKING change needs running a compatible version before it deploys.
    ``cyclic_services`` are the services in, or blocked behind, a dependency
    cycle (DEPENDENCY_CYCLE); the order shown for them breaks it arbitrarily.
    """

    approved: bool
    actionable: bool
    reason_codes: List[ReasonCode] = Field(default_factory=list)
    risk_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    safe_order_established: bool = False
    deployment_sequence: List[str] = Field(min_length=1)
    services_to_deploy: List[str] = Field(default_factory=list)
    stale_consumers: List[str] = Field(default_factory=list)
    cyclic_services: List[str] = Field(default_factory=list)
    justification: str = Field(min_length=1)
    advisory_assessment: Optional[RiskAssessment] = None
    evidence: AssessmentEvidence
    issues: List[EvidenceIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _verdict_follows_reason_codes(self) -> Self:
        limited = any(code.limits_actionability for code in self.reason_codes)
        if self.actionable == limited:
            raise ValueError(
                "actionable must be false exactly when a reason code limits it"
            )
        if self.approved and self.reason_codes:
            raise ValueError("an approved assessment cannot carry reason codes")
        if self.approved and self.advisory_assessment is None:
            raise ValueError("an approved assessment requires an advisory assessment")
        return self


class RunEventStatus(str, Enum):
    """Outcome of one step shown in a run's live log.

    INFO: context, nothing was checked. PASSED: a check succeeded.
    WARNING: a check found something worth reviewing that doesn't block.
    BLOCKED: a check found a reason the release can't go ahead.
    FAILED: a step could not run (an error, not a finding).
    """

    INFO = "info"
    PASSED = "passed"
    WARNING = "warning"
    BLOCKED = "blocked"
    FAILED = "failed"


class RunEvent(BaseModel):
    """One plain-language line in a run's live log.

    ``seq`` is the event's position in its run (1-based), so a client can
    ask for everything after the last one it saw. ``stage`` names the step
    (e.g. "Kubernetes scan"), ``message`` says what was checked and found,
    and ``details`` holds supporting lines (IDs, errors, counts).
    """

    seq: int = Field(ge=1)
    at: datetime
    stage: str = Field(min_length=1)
    status: RunEventStatus
    message: str = Field(min_length=1)
    details: List[str] = Field(default_factory=list)


RunKind = Literal["onboarding", "assessment"]
RunState = Literal["running", "completed", "failed"]


class RunSummary(BaseModel):
    """One onboarding or PR assessment run, as listed on the Runs page.

    ``outcome`` is a one-line result once the run ends (for example
    "Baseline 2026...-abcd saved" or "Blocked: DISCOVERY_GAP").
    """

    run_id: str = Field(min_length=1)
    kind: RunKind
    title: str
    tenant: str
    project_id: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    state: RunState = "running"
    outcome: str = ""
    event_count: int = Field(ge=0, default=0)

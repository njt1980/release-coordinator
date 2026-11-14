"""Core domain models for the enterprise service dependency graph."""

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Dict, Final, Generic, List, Literal, Optional, Self, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


T = TypeVar("T")


class DiscoveryMethod(Enum):
    """Source used to discover a dependency between services."""

    STATIC_CODE = "STATIC_CODE"
    RUNTIME_TELEMETRY = "RUNTIME_TELEMETRY"
    K8S_MANIFEST = "K8S_MANIFEST"


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


class ServiceNode(BaseModel):
    """A microservice node and its identifying metadata in the dependency graph.

    ``service_id`` is the service's FQURI (see ``ServiceIdentity``).
    """

    service_id: str
    name: str
    repository_url: Optional[str] = None
    criticality_tier: int = 3
    metadata: Dict[str, str] = Field(default_factory=dict)

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
    may be set. Together they let a variable be proven unset: absent from
    all three.
    """

    service_id: str
    variables: Dict[str, str] = Field(default_factory=dict)
    variable_sources: Dict[str, List[str]] = Field(default_factory=dict)
    opaque_variables: List[str] = Field(default_factory=list)
    has_opaque_sources: bool = False

    def is_unset(self, variable: str) -> bool:
        """Whether ``variable`` is provably not set for this service."""
        return not (
            self.has_opaque_sources
            or variable in self.variables
            or variable in self.opaque_variables
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
    (None when unfiltered). Both are checked against the project policy, so
    the ``cluster`` label is backed by what was really read.
    """

    environment: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
    cluster: str = Field(pattern=IDENTITY_SEGMENT_PATTERN)
    namespaces: List[str] = Field(default_factory=list)
    telemetry_workspace_id: str = ""
    scanned_revisions: List[ScannedRevision] = Field(default_factory=list)
    kubernetes_api_server: str = ""
    telemetry_cluster: Optional[TelemetryClusterFilter] = None

    @field_validator("kubernetes_api_server")
    @classmethod
    def _normalize_api_server(cls, value: str) -> str:
        return normalize_api_server(value) if value else ""

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

    @model_validator(mode="after")
    def _mock_fixtures_carry_no_confidence(self) -> Self:
        if self.provenance == MOCK_FIXTURE_PROVENANCE and self.confidence_score != 0.0:
            raise ValueError("mock_fixture edges must have a confidence_score of 0.0")
        return self

    @property
    def is_mock_fixture(self) -> bool:
        """Whether any of this edge's evidence comes from a mock fixture."""
        return MOCK_FIXTURE_PROVENANCE in self.provenance.split(LABEL_SEPARATOR)


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
    """

    services: List[ServiceNode]
    edges: List[DependencyEdge]
    discovery: Optional[List[DiscoveryStageReport]] = None
    unresolved_entities: List[UnresolvedEntity] = Field(default_factory=list)
    scan: Optional[ScanContext] = None

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
    """An unresolved node reached by the radius traversal, and why it matters."""

    node: str
    detail: str


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
    """

    edge_count: int = Field(ge=0)
    newest_observed_at: Optional[datetime] = None
    observed_services: List[str] = Field(default_factory=list)
    uncovered_services: List[str] = Field(default_factory=list)
    silent_services: List[str] = Field(default_factory=list)
    service_last_seen: Dict[str, datetime] = Field(default_factory=dict)
    expired_exemptions: List[str] = Field(default_factory=list)


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
    ``coverage`` and ``dropped_records`` describe the live query.
    """

    source: Literal["knowledge_graph", "live", "mock"]
    runtime_evidence: RuntimeEvidenceState = RuntimeEvidenceState.NONE
    runtime_observed_edges: List[str] = Field(default_factory=list)
    baseline_runtime_edges: List[str] = Field(default_factory=list)
    runtime_metrics: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    unbaselined_edges: List[ObservedEdge] = Field(default_factory=list)
    coverage: Optional[LiveCoverage] = None
    dropped_records: Dict[str, int] = Field(default_factory=dict)


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
        change type; no contract compatibility evidence proves it safe.
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

    Every code except MODEL_REJECTED makes the assessment non-actionable: the
    evidence is insufficient to rely on the verdict, which is different from
    a fully evidenced verdict that the release is too risky.
    """

    BASELINE_UNAVAILABLE = "BASELINE_UNAVAILABLE"
    DISCOVERY_GAP = "DISCOVERY_GAP"
    UNMAPPED_FILES = "UNMAPPED_FILES"
    DEMO_EVIDENCE = "DEMO_EVIDENCE"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    COMPATIBILITY_UNVERIFIED = "COMPATIBILITY_UNVERIFIED"
    MODEL_REJECTED = "MODEL_REJECTED"
    POLICY_MISSING = "POLICY_MISSING"
    POLICY_INVALID = "POLICY_INVALID"
    BASELINE_STALE = "BASELINE_STALE"
    REQUIRED_SOURCE_MISSING = "REQUIRED_SOURCE_MISSING"
    BASELINE_DRIFT = "BASELINE_DRIFT"
    BASELINE_SCOPE_MISMATCH = "BASELINE_SCOPE_MISMATCH"

    @property
    def limits_actionability(self) -> bool:
        """Whether this reason makes the assessment ineligible for operational use."""
        return self is not ReasonCode.MODEL_REJECTED


class EvidenceIssue(BaseModel):
    """One reason the evidence behind an assessment is insufficient."""

    code: ReasonCode
    detail: str = Field(min_length=1)


class Exemption(BaseModel):
    """A changed file or unresolved dependency a project policy exempted.

    ``subject`` is the file path or unresolved entity ID; ``rule`` is the
    policy pattern or entity ID that matched it, and ``reason`` the policy's
    justification for the exemption.
    """

    subject: str = Field(min_length=1)
    rule: str = Field(min_length=1)
    reason: str = Field(min_length=1)


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
    the release radius missing from the baseline.
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
    """

    approved: bool
    actionable: bool
    reason_codes: List[ReasonCode] = Field(default_factory=list)
    risk_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    safe_order_established: bool = False
    deployment_sequence: List[str] = Field(min_length=1)
    services_to_deploy: List[str] = Field(default_factory=list)
    stale_consumers: List[str] = Field(default_factory=list)
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

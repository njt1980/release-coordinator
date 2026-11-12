"""Core domain models for the enterprise service dependency graph."""

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Dict, Final, Generic, List, Literal, Optional, Self, TypeVar

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
    """

    service_id: str
    variables: Dict[str, str] = Field(default_factory=dict)

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
    """

    IP_ADDRESS = "IP_ADDRESS"
    PRIVATE_DNS = "PRIVATE_DNS"
    EXTERNAL_DNS = "EXTERNAL_DNS"
    UNREGISTERED_SERVICE = "UNREGISTERED_SERVICE"
    ENVIRONMENT_VARIABLE = "ENVIRONMENT_VARIABLE"


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


class DependencyEdge(BaseModel):
    """A directed, evidence-backed relationship between two service nodes.

    ``provenance`` names where the evidence came from (for example
    ``azure_devops``, ``azure_monitor`` or ``mock_fixture``) and
    ``environment`` the deployment environment it was observed in. Merged
    edges carry comma-separated labels. Both default to ``unknown`` so
    snapshots written before they existed still load. Mock fixture evidence
    is demo data, never real evidence, so it must carry zero confidence.
    """

    source_service_id: str
    target_service_id: str
    discovery_methods: List[DiscoveryMethod]
    confidence_score: float = Field(ge=0.0, le=1.0)
    last_observed_at: datetime
    provenance: str = Field(default=UNKNOWN_LABEL, min_length=1)
    environment: str = Field(default=UNKNOWN_LABEL, min_length=1)

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
    """

    services: List[ServiceNode]
    edges: List[DependencyEdge]
    discovery: Optional[List[DiscoveryStageReport]] = None
    unresolved_entities: List[UnresolvedEntity] = Field(default_factory=list)

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
    and decides the deterministic deployment order.
    """

    pr_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    target_service: str = Field(min_length=1)
    change_type: ContractChange
    source_branch: str = ""
    target_branch: str = "main"
    description: str = ""
    diff: str = Field(min_length=1)

    @field_validator("target_service")
    @classmethod
    def _target_is_fquri(cls, value: str) -> str:
        ServiceIdentity.parse(value)
        return value


class TelemetryStatus(BaseModel):
    """Runtime evidence available for the services in a release radius.

    ``source`` is ``knowledge_graph`` when the evidence comes only from the
    RUNTIME_TELEMETRY edges persisted by discovery, or ``live`` when a
    telemetry workspace was queried for this assessment.
    """

    source: Literal["knowledge_graph", "live"]
    runtime_observed_edges: List[str] = Field(default_factory=list)
    runtime_metrics: Dict[str, Dict[str, float]] = Field(default_factory=dict)


class RiskAssessment(BaseModel):
    """LLM-produced release risk verdict for a pull request.

    The deployment sequence is computed deterministically before the LLM is
    called, so the model only scores, approves and justifies it.
    """

    risk_score: float = Field(ge=0.0, le=1.0)
    approved: bool
    justification: str = Field(min_length=1)


class ReleaseAssessment(BaseModel):
    """Machine-readable release verdict returned to the control plane.

    ``deployment_sequence`` orders the whole release subgraph;
    ``services_to_deploy`` is the part of it the pull request changes, in the
    same order. ``stale_consumers`` lists unchanged upstream consumers that a
    BREAKING change needs running a compatible version before it deploys.
    """

    risk_score: float = Field(ge=0.0, le=1.0)
    approved: bool
    deployment_sequence: List[str] = Field(min_length=1)
    services_to_deploy: List[str] = Field(default_factory=list)
    stale_consumers: List[str] = Field(default_factory=list)
    justification: str = Field(min_length=1)

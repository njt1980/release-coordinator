"""Core domain models for the enterprise service dependency graph."""

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Dict, Generic, List, Literal, Optional, Self, TypeVar

from pydantic import BaseModel, Field, model_validator


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


class ServiceNode(BaseModel):
    """A microservice node and its identifying metadata in the dependency graph."""

    service_id: str
    name: str
    repository_url: Optional[str] = None
    criticality_tier: int = 3
    metadata: Dict[str, str] = Field(default_factory=dict)


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


class DependencyEdge(BaseModel):
    """A directed, evidence-backed relationship between two service nodes."""

    source_service_id: str
    target_service_id: str
    discovery_methods: List[DiscoveryMethod]
    confidence_score: float = Field(ge=0.0, le=1.0)
    last_observed_at: datetime


class KnowledgeGraph(BaseModel):
    """Shape of the persisted ``knowledge_graph.json`` baseline.

    ``discovery`` is None for graphs written before discovery outcomes were
    recorded; such a graph cannot prove its discovery was complete.
    """

    services: List[ServiceNode]
    edges: List[DependencyEdge]
    discovery: Optional[List[DiscoveryStageReport]] = None


class PullRequestPayload(BaseModel):
    """A pull request that triggers a release impact assessment."""

    pr_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    target_service: str = Field(min_length=1)
    source_branch: str = ""
    target_branch: str = "main"
    description: str = ""
    diff: str = Field(min_length=1)


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
    """LLM-produced release risk verdict for a pull request."""

    risk_score: float = Field(ge=0.0, le=1.0)
    approved: bool
    deployment_order: List[str] = Field(min_length=1)
    justification: str = Field(min_length=1)


class ReleaseAssessment(BaseModel):
    """Machine-readable release verdict returned to the control plane."""

    risk_score: float = Field(ge=0.0, le=1.0)
    approved: bool
    deployment_sequence: List[str] = Field(min_length=1)
    justification: str = Field(min_length=1)

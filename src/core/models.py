"""Core domain models for the enterprise service dependency graph."""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class DiscoveryMethod(Enum):
    """Source used to discover a dependency between services."""

    STATIC_CODE = "STATIC_CODE"
    RUNTIME_TELEMETRY = "RUNTIME_TELEMETRY"
    K8S_MANIFEST = "K8S_MANIFEST"


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


class DependencyEdge(BaseModel):
    """A directed, evidence-backed relationship between two service nodes."""

    source_service_id: str
    target_service_id: str
    discovery_methods: List[DiscoveryMethod]
    confidence_score: float = Field(ge=0.0, le=1.0)
    last_observed_at: datetime


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

"""Core domain models for the enterprise service dependency graph."""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

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

"""State schema for release orchestration."""

from typing import TypedDict


class ReleaseGraphState(TypedDict):
    """Data passed between nodes in the release coordination graph."""

    trigger_event: str
    target_services: list[str]
    static_dependents: list[str]
    runtime_metrics: dict[str, object]
    risk_score: float
    deployment_sequence: list[str]
    evidence_report: str

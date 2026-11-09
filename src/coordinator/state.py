"""State schema for release orchestration."""

from typing import TypedDict


class ReleaseGraphState(TypedDict):
    """Data passed between nodes in the release coordination graph."""

    trigger_event: str
    target_services: list[str]
    static_dependents: list[str]
    runtime_metrics: dict[str, object]
    risk_score: float
    evidence_report: str

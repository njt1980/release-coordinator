"""Claude-backed adapter for Day-Two release risk reasoning.

This module is the boundary between the release coordinator and the
Anthropic Messages API: it takes a typed ``RiskAssessmentRequest`` and returns
a validated ``RiskAssessment``. Credentials are resolved by the Anthropic SDK
(``ANTHROPIC_API_KEY`` or an ``ant auth login`` profile).
"""

import os

import anthropic
from pydantic import BaseModel, Field

from src.core import config
from src.core.models import (
    DependencyEdge,
    PullRequestPayload,
    RiskAssessment,
    TelemetryStatus,
)

_MAX_TOKENS = 16000
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You are a release-safety reviewer for a microservice platform. You assess the
blast radius of a pull request and decide whether and in what order services
should be deployed.

How to read the dependency graph you are given:
- Each edge "A -> B" means service A calls / depends on service B.
- Upstream services depend on the target service (they call it). A breaking
  change in the target's API, events or data contracts breaks them.
- Downstream services are called by the target. The target's new code may
  require them to already be deployed and compatible.
- Edges carry discovery evidence: STATIC_CODE (referenced in source),
  K8S_MANIFEST, or RUNTIME_TELEMETRY (observed in live traffic). Runtime
  evidence is the strongest signal that a dependency is real and active.

Scoring guidance for risk_score (0.0 to 1.0):
- Low (< 0.3): internal refactors, no contract change, few or no upstream
  consumers, healthy telemetry.
- Medium (0.3 to 0.7): additive API/schema changes, or several consumers, or
  degraded telemetry on services in the radius.
- High (> 0.7): breaking API, message or database schema changes with active
  upstream consumers, or elevated error rates in the radius.

deployment_order rules:
- Use only service IDs that appear in the request's target, upstream or
  downstream lists, spelled exactly as given; include every target service.
- Providers before consumers: downstream dependencies the change relies on,
  then the target, then upstream consumers that must adopt the change.
- The request includes a dependency-safe baseline order; deviate from it only
  when the diff gives a concrete reason, and say why.

justification: 3-6 sentences that name the specific upstream/downstream
services affected, what in the diff affects them, and how telemetry influenced
the score. Do not invent services, endpoints or metrics that are not in the
request."""


class RiskAssessmentRequest(BaseModel):
    """Everything the reasoner needs to assess one pull request."""

    pull_request: PullRequestPayload
    target_services: list[str] = Field(min_length=1)
    upstream_services: list[str]
    downstream_services: list[str]
    baseline_deployment_order: list[str]
    matched_edges: list[DependencyEdge]
    telemetry: TelemetryStatus


class RiskReasoningError(RuntimeError):
    """The model did not return a usable risk assessment."""


def _render_request(request: RiskAssessmentRequest) -> str:
    """Render the request as JSON context followed by the raw diff."""
    context = request.model_dump_json(
        indent=2, exclude={"pull_request": {"diff"}}
    )
    return (
        "Assess the release risk of this pull request.\n\n"
        f"<context>\n{context}\n</context>\n\n"
        f"<diff>\n{request.pull_request.diff}\n</diff>"
    )


def validate_assessment(
    assessment: RiskAssessment, request: RiskAssessmentRequest
) -> RiskAssessment:
    """Reject deployment orders that drop targets or name unknown services."""
    allowed = {
        *request.target_services,
        *request.upstream_services,
        *request.downstream_services,
    }
    unknown = [service for service in assessment.deployment_order if service not in allowed]
    if unknown:
        raise RiskReasoningError(
            f"deployment_order names services outside the release radius: {unknown}"
        )
    if len(set(assessment.deployment_order)) != len(assessment.deployment_order):
        raise RiskReasoningError("deployment_order contains duplicate services")
    missing = [
        service
        for service in request.target_services
        if service not in assessment.deployment_order
    ]
    if missing:
        raise RiskReasoningError(
            f"deployment_order omits target services: {missing}"
        )
    return assessment


class RiskReasoner:
    """Assess pull-request risk with a Claude model and structured output."""

    def __init__(
        self,
        model: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        """Bind the reasoner to a model and Anthropic client.

        Args:
            model: Claude model ID. Defaults to RISK_REASONER_MODEL from the
                environment, else ``config.DEFAULT_RISK_REASONER_MODEL``.
            client: Pre-built client (for tests); a default client is created
                otherwise, resolving credentials from the environment.
        """
        self._model = model or os.getenv(
            "RISK_REASONER_MODEL", config.DEFAULT_RISK_REASONER_MODEL
        )
        self._client = client or anthropic.Anthropic()

    def assess(self, request: RiskAssessmentRequest) -> RiskAssessment:
        """Return a validated risk assessment for the request.

        Raises:
            RiskReasoningError: If the model refuses, the output cannot be
                parsed, or the deployment order is inconsistent with the
                release radius.
            anthropic.APIError: On API, authentication, or network failures.
        """
        response = self._client.beta.messages.parse(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _render_request(request)}],
            output_config={"effort": "high"},
            output_format=RiskAssessment,
            betas=[_FALLBACK_BETA],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RiskReasoningError("The model declined to assess this pull request")
        if response.stop_reason == "max_tokens":
            raise RiskReasoningError("The risk assessment was truncated at max_tokens")
        assessment = response.parsed_output
        if assessment is None:
            raise RiskReasoningError("The model returned no structured assessment")
        return validate_assessment(assessment, request)

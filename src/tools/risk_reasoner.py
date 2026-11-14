"""Claude-backed adapter for Day-Two release risk reasoning.

This module is the boundary between the release coordinator and the
Anthropic Messages API: it takes a typed ``RiskAssessmentRequest`` and returns
a validated ``RiskAssessment``. Credentials are resolved by the Anthropic SDK
(``ANTHROPIC_API_KEY`` or an ``ant auth login`` profile).
"""

import os

import anthropic
from pydantic import BaseModel, Field

from src.core.redaction import redact
from src.core import config
from src.core.models import (
    ContractChange,
    DependencyEdge,
    PullRequestPayload,
    RiskAssessment,
    TelemetryStatus,
)

_MAX_TOKENS = 16000
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You are a release-safety reviewer for a microservice platform. You are given
a pull request, its dependency blast radius, and a deployment sequence that
has ALREADY been computed deterministically. You do not choose or change the
deployment order. Your only job is to explain why the provided sequence is
required, and to output risk_score and approved.

How to read the dependency graph you are given:
- Each edge "A -> B" means service A calls / depends on service B.
- Upstream services depend on the target service, directly or transitively
  (they call it, or call a service that does). A breaking change in the
  target's API, events or data contracts can break them.
- Downstream services are called by the target, directly or transitively.
  The target's new code may require them to already be deployed and
  compatible.
- Service IDs are URIs of the form
  urn:release-coord:{tenant}:{environment}:{cluster}:{namespace}:{name}.
- Edges carry discovery evidence: STATIC_CODE (referenced in source),
  K8S_MANIFEST, or RUNTIME_TELEMETRY (observed in live traffic). Runtime
  evidence is the strongest signal that a dependency is real and active.
- Edges also carry provenance. Provenance "mock_fixture" (confidence 0.0)
  is demo fixture data, not observed traffic: do not treat it as evidence.

How the deployment_sequence was computed (a topological sort of the
subgraph, from the declared change_type):
- additive: providers before consumers. Downstream dependencies first, then
  the target, then upstream consumers that may start using the new
  capability.
- breaking: consumers before providers. Upstream consumers first, so they
  already send any newly required input or accept the new message format,
  then the target, then downstream dependencies.
- deployment_sequence covers the whole subgraph; services_to_deploy is the
  part of it this pull request changes (the targets). The other services
  keep running their current versions.
- stale_consumers (breaking only) are upstream consumers this pull request
  does not change, yet the sequence needs them on a compatible version before
  the target deploys. Unless the diff or description shows they are already
  compatible (for example, updated by an earlier release), treat them as
  consumers with no compatible version to deploy first.

Scoring guidance for risk_score (0.0 to 1.0):
- Low (< 0.3): internal refactors, no contract change, few or no upstream
  consumers, healthy telemetry.
- Medium (0.3 to 0.7): additive API/schema changes, or several consumers, or
  degraded telemetry on services in the radius.
- High (> 0.7): breaking API, message or database schema changes with active
  upstream consumers, or elevated error rates in the radius.

approved: true when the release can proceed safely by following the
provided deployment_sequence. Set it to false when the change should be held
for human review instead, in particular when:
- the diff contradicts the declared change_type (for example, declared
  additive but the diff makes a field required, removes or renames a field
  or endpoint, tightens validation, or publishes a new message/schema
  version; or declared breaking but the diff only adds optional
  capability), because the provided sequence would then be wrong;
- a breaking change's affected consumers have no compatible version to
  deploy first;
- error rates are elevated on services in the radius;
- the diff is too unclear to assess.
The justification must state the reason when approved is false.

justification: 3-6 sentences that explain why the provided
deployment_sequence is required given the diff and the graph: name the
specific upstream/downstream services affected, what in the diff affects
them, why each must deploy before or after the target, and how telemetry
influenced the score. Refer to the sequence exactly as given; never propose
a different order. Do not invent services, endpoints or metrics that are
not in the request."""


class RiskAssessmentRequest(BaseModel):
    """Everything the reasoner needs to assess one pull request.

    ``deployment_sequence`` is the deterministic order computed before the
    LLM is called; the model justifies it but cannot change it.
    ``services_to_deploy`` and ``stale_consumers`` come from the same plan.
    """

    pull_request: PullRequestPayload
    change_type: ContractChange
    target_services: list[str] = Field(min_length=1)
    upstream_services: list[str]
    downstream_services: list[str]
    deployment_sequence: list[str] = Field(min_length=1)
    services_to_deploy: list[str] = Field(default_factory=list)
    stale_consumers: list[str] = Field(default_factory=list)
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
        "Assess the release risk of this pull request and justify the "
        "provided deployment_sequence.\n\n"
        f"<context>\n{context}\n</context>\n\n"
        f"<diff>\n{request.pull_request.diff}\n</diff>"
    )


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
        self._model: str = (
            model
            or os.environ.get("RISK_REASONER_MODEL")
            or config.DEFAULT_RISK_REASONER_MODEL
        )
        self._client = client or anthropic.Anthropic()
        self.last_redactions: dict[str, int] = {}

    def assess(self, request: RiskAssessmentRequest) -> RiskAssessment:
        """Return a validated risk assessment for the request.

        Everything sent is redacted first (``core.redaction``); what was
        removed is counted in ``last_redactions``.

        Raises:
            RiskReasoningError: If the model refuses or the output cannot be
                parsed.
            anthropic.APIError: On API, authentication, or network failures.
        """
        redacted = redact(_render_request(request))
        self.last_redactions = redacted.counts
        response = self._client.beta.messages.parse(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": redacted.text}],
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
        return assessment

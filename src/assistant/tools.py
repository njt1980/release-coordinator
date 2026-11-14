"""Read-only tools the assistant may call to answer from the platform's evidence.

Each tool has a typed input model, reads one existing store or registry,
and returns JSON text. None of them changes anything: the assistant can
explain and draft, never approve, exempt or edit. Tool output is data
gathered from repositories, clusters and telemetry, and is passed to the
model as data, never as instructions.
"""

import json
from collections.abc import Callable
from typing import Any, Final

from anthropic.types.beta import BetaToolParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.core.reason_explanations import REASON_EXPLANATIONS
from src.core.models import ReasonCode, ServiceIdentity
from src.orchestration.run_log import DEFAULT_RUN_LOGS, RunLogStore
from src.tools.baseline_store import (
    PATH_SEGMENT_PATTERN,
    BaselineScope,
    BaselineStore,
    default_store,
)
from src.tools.policy_store import (
    PolicyInvalidError,
    PolicyMissingError,
    PolicyStore,
    default_policy_store,
)

# Tool results are capped so one large baseline can't crowd out the conversation.
MAX_RESULT_CHARS: Final[int] = 20_000


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectInput(_Input):
    tenant: str = Field(pattern=PATH_SEGMENT_PATTERN, description="Tenant, e.g. 'default'.")
    project_id: str = Field(pattern=PATH_SEGMENT_PATTERN, description="Project, e.g. 'default'.")


class ServiceInput(ProjectInput):
    service: str = Field(
        min_length=1,
        max_length=300,
        description="Service name ('order-service'), '<namespace>/<name>' or a full FQURI.",
    )


class RunInput(_Input):
    run_id: str = Field(min_length=1, max_length=100, description="Run or job ID from list_runs.")


class NoInput(_Input):
    pass


class ReasonInput(_Input):
    code: str = Field(min_length=1, max_length=60, description="A reason code, e.g. 'DISCOVERY_GAP'.")




def _json(value: Any) -> str:
    text = json.dumps(value, default=str, indent=1)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + "\n... (truncated)"
    return text


def _short(node_id: str) -> str:
    try:
        identity = ServiceIdentity.parse(node_id)
    except ValueError:
        return node_id
    return f"{identity.namespace}/{identity.service_name}"


class AssistantTools:
    """The read-only tools, bound to the stores they read."""

    def __init__(
        self,
        baselines: BaselineStore | None = None,
        policies: PolicyStore | None = None,
        runs: RunLogStore | None = None,
        find_assessment: Callable[[str], Any] | None = None,
    ) -> None:
        self._baselines = baselines or default_store()
        self._policies = policies or default_policy_store()
        self._runs = runs or DEFAULT_RUN_LOGS
        self._find_assessment = find_assessment

    # Each tool: (input model, handler, description)
    def specs(self) -> dict[str, tuple[type[_Input], Callable[[Any], str], str]]:
        return {
            "get_baseline_overview": (
                ProjectInput,
                self.baseline_overview,
                "The project's newest baseline: what it scanned (environment, cluster, API "
                "server, telemetry filter, revisions), per-source discovery results, the "
                "services, and the unresolved endpoints with their kinds.",
            ),
            "get_service": (
                ServiceInput,
                self.service,
                "One service in the newest baseline: its repository directory, what it calls "
                "and what calls it, and which evidence (code, Kubernetes, telemetry) found "
                "each dependency.",
            ),
            "get_policy": (
                ProjectInput,
                self.policy,
                "The project's release policy (validated), or why it is missing or invalid.",
            ),
            "list_runs": (
                NoInput,
                self.list_runs,
                "Recent onboarding and PR assessment runs, newest first, with outcomes.",
            ),
            "get_run_log": (
                RunInput,
                self.run_log,
                "Every step of one run: what was checked and what it found.",
            ),
            "get_assessment": (
                RunInput,
                self.assessment,
                "The verdict of one PR assessment: approval, reason codes, deployment order, "
                "radius and the evidence it was based on.",
            ),
            "explain_reason_code": (
                ReasonInput,
                self.explain_reason,
                "What a blocking reason code means and how it is usually resolved.",
            ),
        }

    def definitions(self) -> list[BetaToolParam]:
        """Tool definitions for the Messages API.

        Inputs are validated against the Pydantic models when a tool runs
        (``run``), so the schemas don't need strict mode, which supports
        fewer JSON Schema keywords.
        """
        definitions: list[BetaToolParam] = []
        for name, (model, _, description) in self.specs().items():
            schema = model.model_json_schema()
            schema.pop("title", None)
            schema["additionalProperties"] = False
            schema.setdefault("properties", {})
            schema["required"] = list(schema["properties"])
            definitions.append(
                BetaToolParam(name=name, description=description, input_schema=schema)
            )
        return definitions

    def run(self, name: str, raw_input: Any) -> tuple[str, bool]:
        """Run tool ``name``; returns (result text, is_error)."""
        spec = self.specs().get(name)
        if spec is None:
            return f"Unknown tool {name!r}.", True
        model, handler, _ = spec
        try:
            arguments = model.model_validate(raw_input)
        except ValidationError as error:
            return f"Invalid input: {error}", True
        try:
            return handler(arguments), False
        except (OSError, ValueError, LookupError) as error:
            return f"{name} failed: {error}", True

    # --- handlers ---------------------------------------------------------

    def _latest(self, arguments: ProjectInput) -> Any:
        scope = BaselineScope(tenant=arguments.tenant, project_id=arguments.project_id)
        baseline = self._baselines.latest(scope)
        if baseline is None:
            raise LookupError(f"no complete baseline for {scope.tenant}/{scope.project_id}")
        return baseline

    def baseline_overview(self, arguments: ProjectInput) -> str:
        baseline = self._latest(arguments)
        graph = baseline.graph
        return _json(
            {
                "snapshot_id": baseline.snapshot_id,
                "created_at": baseline.created_at,
                "demo_mode": baseline.demo_mode,
                "scan": graph.scan.model_dump(mode="json") if graph.scan else None,
                "discovery": [r.model_dump(mode="json") for r in graph.discovery or []],
                "services": sorted(_short(s.service_id) for s in graph.services),
                "dependency_count": len(graph.edges),
                "unresolved": [
                    {"entity_id": e.entity_id, "kind": e.kind.value} for e in graph.unresolved_entities
                ],
            }
        )

    def service(self, arguments: ServiceInput) -> str:
        baseline = self._latest(arguments)
        wanted = arguments.service.strip().lower()
        matches = [
            s
            for s in baseline.graph.services
            if wanted in (s.service_id.lower(), _short(s.service_id), s.name.lower())
        ]
        if len(matches) != 1:
            names = sorted(_short(s.service_id) for s in baseline.graph.services)
            raise LookupError(
                f"{arguments.service!r} matches {len(matches)} services; known services: {names}"
            )
        node = matches[0]

        def edge(e: Any, other: str) -> dict[str, Any]:
            return {
                "service": _short(other),
                "found_by": [m.value for m in e.discovery_methods],
                "last_observed_at": e.last_observed_at,
            }

        return _json(
            {
                "service": _short(node.service_id),
                "fquri": node.service_id,
                "metadata": node.metadata,
                "calls": [edge(e, e.target_service_id) for e in baseline.graph.edges if e.source_service_id == node.service_id],
                "called_by": [edge(e, e.source_service_id) for e in baseline.graph.edges if e.target_service_id == node.service_id],
                "baseline_snapshot_id": baseline.snapshot_id,
            }
        )

    def policy(self, arguments: ProjectInput) -> str:
        scope = BaselineScope(tenant=arguments.tenant, project_id=arguments.project_id)
        try:
            loaded = self._policies.load(scope)
        except (PolicyMissingError, PolicyInvalidError) as error:
            return _json({"status": type(error).__name__, "detail": str(error)})
        return _json(
            {
                "status": "valid",
                "path": f"policies/{scope.tenant}/{scope.project_id}.yaml",
                "sha256": loaded.sha256,
                "policy": loaded.policy.model_dump(mode="json"),
            }
        )

    def list_runs(self, arguments: NoInput) -> str:
        return _json([summary.model_dump(mode="json") for summary in self._runs.summaries()])

    def run_log(self, arguments: RunInput) -> str:
        found = self._runs.get(arguments.run_id)
        if found is None:
            raise LookupError(f"no run {arguments.run_id!r} (logs are cleared on restart)")
        summary, events = found
        return _json(
            {
                "run": summary.model_dump(mode="json"),
                "events": [
                    {
                        "seq": e.seq,
                        "stage": e.stage,
                        "status": e.status.value,
                        "message": e.message,
                        "details": e.details,
                    }
                    for e in events
                ],
            }
        )

    def assessment(self, arguments: RunInput) -> str:
        record = self._find_assessment(arguments.run_id) if self._find_assessment else None
        if record is None:
            raise LookupError(f"no PR assessment {arguments.run_id!r} (cleared on restart)")
        return _json(record.model_dump(mode="json", exclude={"evidence_report"}))

    def explain_reason(self, arguments: ReasonInput) -> str:
        try:
            code = ReasonCode(arguments.code.strip().upper())
        except ValueError:
            raise LookupError(
                f"unknown reason code {arguments.code!r}; known: {[c.value for c in ReasonCode]}"
            ) from None
        meaning, fix = REASON_EXPLANATIONS.get(code, ("See docs/release-model.md.", ""))
        return _json({"code": code.value, "meaning": meaning, "usual_fix": fix})

"""Score scanner output against an app's documented dependency graph.

Two edge modes:

* ``static``: literal URL references only (no runtime environment).
* ``manifest``: static plus every env-variable reference resolved through
  ``config_resolution.resolve_variable`` against env read from the app's
  deployment files (an emulation of the Kubernetes source), plus edges the
  env itself declares (``edge_resolution.environment_addresses``).

A missed edge is *flagged* when its caller reports incomplete coverage
(anything unscanned or not understood) and *silent* when the caller claims
complete coverage: silent misses are the fail-closed headline.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from src.core.config_resolution import ResolutionOutcome, resolve_variable
from src.core.edge_resolution import environment_addresses, shared_config_maps
from src.core.env_names import endpoint_of_value
from src.core.models import ServiceEnvironment
from src.core.service_ids import host_of
from src.tools.devops_extractor import ReferenceKind, RepositoryScan, ServiceScan

from .env_sources import WorkloadEnv
from .models import (
    NOISE,
    OPTIONAL,
    AppResult,
    AppTruth,
    EdgeMode,
    EdgeScores,
    EdgeStatus,
    Headline,
    InventoryScore,
    MissAccounting,
    NodeKind,
    Score,
)

Pair = tuple[str, str]
_CLUSTER_SUFFIXES = (".svc.cluster.local", ".svc")


def score(tp: int, fp: int, fn: int) -> Score:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return Score(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=None if precision is None else round(precision, 3),
        recall=None if recall is None else round(recall, 3),
    )


def normalize_host(raw: str) -> str:
    """Lower-case host with in-cluster DNS suffixes reduced to the Service name."""
    host = host_of(raw) if raw else ""
    for suffix in _CLUSTER_SUFFIXES:
        if host.endswith(suffix):
            host = host[: -len(suffix)].split(".")[0]
    return host


@dataclass
class MatchedService:
    key: str
    truth_name: str | None
    scan: ServiceScan


@dataclass
class ServiceMatch:
    services: list[MatchedService] = field(default_factory=list)
    bogus: list[dict[str, str]] = field(default_factory=list)
    nontrivial: list[str] = field(default_factory=list)


def match_services(truth: AppTruth, scans: Iterable[tuple[str, RepositoryScan]]) -> ServiceMatch:
    """Link each scanned service to a ground-truth service through the mapping."""
    boundaries = truth.mapping.boundaries
    match = ServiceMatch()
    for repo, scan in scans:
        for service in scan.services:
            name = service.service.name
            key = f"{repo}:{name}"
            default = name if name in truth.services else None
            by_root = boundaries.get(f"root:{repo}:{service.service.root}", default)
            target = boundaries.get(key, boundaries.get(name, by_root))
            if target == OPTIONAL:
                continue
            if target is None:
                match.bogus.append({"boundary": key, "root": service.service.root})
            elif target != name:
                match.nontrivial.append(f"{name} -> {target}")
            match.services.append(MatchedService(key, target, service))
    return match


def inventory_score(truth: AppTruth, match: ServiceMatch) -> InventoryScore:
    expected = {
        name
        for name, node in truth.services.items()
        if node.kind is NodeKind.SERVICE and node.source and not node.optional
    }
    found = {s.truth_name for s in match.services if s.truth_name}
    base = score(len(found & expected), len(match.bogus), len(expected - found))
    return InventoryScore(
        **base.model_dump(),
        missing=sorted(expected - found),
        bogus=match.bogus,
        nontrivial_mappings=match.nontrivial,
    )


def _environment(name: str, env: WorkloadEnv) -> ServiceEnvironment:
    return ServiceEnvironment(
        service_id=f"urn:release-coord:bench:bench:bench:default:{name}",
        variables=env.variables,
        variable_sources={
            variable: [f"bench/{name}:" + (f"configMap/{env.bulk[variable]}" if variable in env.bulk else "env")]
            for variable in env.variables
        },
        opaque_variables=sorted(set(env.opaque)),
        has_opaque_sources=env.has_opaque_sources,
    )


class _Predictor:
    def __init__(self, truth: AppTruth, envs: dict[str, WorkloadEnv], mode: EdgeMode) -> None:
        self.truth = truth
        self.envs = envs
        self.mode = mode
        self.edges: dict[Pair, list[str]] = {}
        self.unresolved: list[dict[str, str]] = []

    def callee(self, host: str) -> str | None:
        hosts = self.truth.mapping.hosts
        if host in hosts:
            return hosts[host]
        known = host in self.truth.services or host in self.truth.observability
        return host if known else None

    def target(self, service: MatchedService, ref_kind: ReferenceKind, endpoint: str) -> tuple[str, str] | None:
        if ref_kind is ReferenceKind.URL:
            return normalize_host(endpoint_of_value(endpoint) or endpoint), ""
        env = self.envs.get(service.truth_name or "")
        if self.mode is not EdgeMode.MANIFEST or env is None or service.truth_name is None:
            return None
        resolution = resolve_variable(
            endpoint, service.scan.config, _environment(service.truth_name, env), None
        )
        usable = (ResolutionOutcome.RUNTIME, ResolutionOutcome.STATIC_DEFAULT)
        if resolution.outcome not in usable or not resolution.endpoint:
            return None
        return normalize_host(resolution.endpoint), f" => {resolution.outcome.value}:{resolution.endpoint}"

    def add_environment(self, service: MatchedService) -> None:
        """Edges the workload's env declares (``environment_edges`` in discovery)."""
        env = self.envs.get(service.truth_name or "")
        if self.mode is not EdgeMode.MANIFEST or env is None or service.truth_name is None:
            return
        shared = shared_config_maps({_environment(n, e).service_id: _environment(n, e) for n, e in self.envs.items()})
        sources = _environment(service.truth_name, env).variable_sources
        for variable, address in environment_addresses(env.variables, sources, {name for _, name in shared}):
            callee = self.callee(normalize_host(address))
            if callee and callee != NOISE and callee not in self.truth.observability:
                if callee != service.truth_name:
                    self.edges.setdefault((service.truth_name, callee), []).append(f"K8S_ENV:{variable}")

    def add_service(self, service: MatchedService) -> None:
        caller = service.truth_name or f"BOGUS:{service.key}"
        for ref in service.scan.references:
            found = self.target(service, ref.kind, ref.endpoint)
            if found is None or not found[0]:
                continue
            host, resolved = found
            how = f"{ref.kind.value}:{ref.endpoint} ({ref.file_path}){resolved}"
            callee = self.callee(host)
            if callee == NOISE or callee in self.truth.observability:
                continue
            if callee is None:
                self.unresolved.append({"from": caller, "host": host, "via": how})
            elif callee != service.truth_name:
                self.edges.setdefault((caller, callee), []).append(how)


def predict_edges(
    truth: AppTruth, match: ServiceMatch, envs: dict[str, WorkloadEnv], mode: EdgeMode
) -> tuple[dict[Pair, list[str]], list[dict[str, str]]]:
    predictor = _Predictor(truth, envs, mode)
    for service in match.services:
        predictor.add_service(service)
        predictor.add_environment(service)
    return predictor.edges, predictor.unresolved


def score_edges(
    truth: AppTruth, predicted: dict[Pair, list[str]], unresolved: list[dict[str, str]]
) -> EdgeScores:
    core = {(e.caller, e.callee): e for e in truth.edges if e.status is EdgeStatus.CORE}
    ignored = {(e.caller, e.callee) for e in truth.edges if e.status is not EdgeStatus.CORE}
    scored = {pair: how for pair, how in predicted.items() if pair not in ignored}

    def is_infra(pair: Pair) -> bool:
        node = truth.services.get(pair[1])
        return node is not None and node.kind is NodeKind.INFRA

    by_kind: dict[str, Score] = {}
    for kind in ("svc", "infra", "all"):
        want = {p for p, e in core.items() if kind == "all" or e.kind == kind}
        got = {p for p in scored if kind == "all" or is_infra(p) == (kind == "infra")}
        extra = len(unresolved) if kind == "all" else 0
        by_kind[kind] = score(len(got & want), len(got - want) + extra, len(want - got))
    return EdgeScores(
        svc=by_kind["svc"],
        infra=by_kind["infra"],
        all=by_kind["all"],
        true_positives=[{"edge": list(p), "via": scored[p]} for p in scored if p in core],
        false_positives=[{"edge": list(p), "via": scored[p]} for p in scored if p not in core],
        unresolved_targets=unresolved,
        missed=[core[p].model_dump(by_alias=True, mode="json") for p in core if p not in scored],
    )


def _causes(match: ServiceMatch) -> dict[str, set[str]]:
    """Cause key (the exact set of unread files) -> services sharing it."""
    causes: dict[str, set[str]] = {}
    for service in match.services:
        coverage = service.scan.coverage
        if service.truth_name and coverage and coverage.unscanned:
            key = "\n".join(sorted(u.path for u in coverage.unscanned))
            causes.setdefault(key, set()).add(service.truth_name)
    return causes


def miss_accounting(match: ServiceMatch, missed: list[dict[str, object]]) -> MissAccounting:
    reasons: dict[str, set[str]] = {}
    for service in match.services:
        if service.truth_name:
            coverage = service.scan.coverage
            found = {u.reason.value for u in coverage.unscanned} if coverage else set()
            reasons.setdefault(service.truth_name, set()).update(found)
    incomplete = {name for name, r in reasons.items() if r}
    callers = [str(edge["from"]) for edge in missed]
    missing_callers = set(callers)
    causes = _causes(match)
    return MissAccounting(
        services_scored=len(reasons),
        services_not_understood=sum("REFERENCES_NOT_UNDERSTOOD" in r for r in reasons.values()),
        services_unsupported_language=sum("UNSUPPORTED_LANGUAGE" in r for r in reasons.values()),
        services_incomplete_any=len(incomplete),
        missed_edges=len(missed),
        missed_flagged=sum(c in incomplete for c in callers),
        missed_silent=sum(c in reasons and c not in incomplete for c in callers),
        missed_caller_has_no_boundary=sum(c not in reasons for c in callers),
        services_flagged_with_miss=len(incomplete & missing_callers),
        causes_flagged=len(causes),
        causes_flagged_with_miss=sum(1 for members in causes.values() if members & missing_callers),
    )


def score_app(
    truth: AppTruth, scans: list[tuple[str, RepositoryScan]], envs: dict[str, WorkloadEnv]
) -> AppResult:
    """Score one app; ``envs`` is keyed by deployment workload name."""
    match = match_services(truth, scans)
    workloads = truth.mapping.workloads
    by_service = {workloads.get(name, name): env for name, env in envs.items()}
    edges = {
        mode: score_edges(truth, *predict_edges(truth, match, by_service, mode))
        for mode in EdgeMode
    }
    return AppResult(
        app=truth.app,
        inventory=inventory_score(truth, match),
        edges=edges,
        misses=miss_accounting(match, edges[EdgeMode.MANIFEST].missed),
    )


def headline(results: list[AppResult]) -> Headline:
    """Micro-averaged (pooled) headline over ``results``."""

    def pooled(scores: list[Score]) -> Score:
        return score(sum(s.tp for s in scores), sum(s.fp for s in scores), sum(s.fn for s in scores))

    flagged = sum(r.misses.services_incomplete_any for r in results)
    useful = sum(r.misses.services_flagged_with_miss for r in results)
    causes = sum(r.misses.causes_flagged for r in results)
    useful_causes = sum(r.misses.causes_flagged_with_miss for r in results)
    inventory = pooled([r.inventory for r in results])
    static = pooled([r.edges[EdgeMode.STATIC].all for r in results])
    manifest = pooled([r.edges[EdgeMode.MANIFEST].all for r in results])
    return Headline(
        inventory_precision=inventory.precision,
        inventory_recall=inventory.recall,
        static_precision=static.precision,
        static_recall=static.recall,
        manifest_precision=manifest.precision,
        manifest_recall=manifest.recall,
        missed_silent=sum(r.misses.missed_silent for r in results),
        missed_flagged=sum(r.misses.missed_flagged for r in results),
        services_flagged=flagged,
        flag_precision=round(useful / flagged, 3) if flagged else None,
        cause_precision=round(useful_causes / causes, 3) if causes else None,
    )

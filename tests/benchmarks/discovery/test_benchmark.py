"""Offline unit tests for the discovery benchmark (part of the default run).

They cover scoring, the ratchet, the env emulation and the integrity of the
committed ground truth; the benchmark itself (network, real repositories)
runs only through ``run_bench``.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from src.core.models import ScannedRevision, ServiceCoverage, UnscannedFile, UnscannedReason
from src.tools.devops_extractor import (
    BoundarySource,
    EndpointReference,
    ReferenceKind,
    RepositoryScan,
    ServiceBoundary,
    ServiceScan,
)

from .env_sources import WorkloadEnv, compose_envs, k8s_envs, substitute
from .models import NOISE, OPTIONAL, AppTruth, BenchReport, EdgeMode, Headline
from .ratchet import regressions
from .run_bench import APPS_DIR, BASELINE, all_apps, load_truth
from .scoring import normalize_host, score, score_app

SHA = "a" * 40


def _service(name: str, urls: list[str], incomplete: bool = False, env_vars: list[str] | None = None) -> ServiceScan:
    refs = [
        EndpointReference(service_name=name, endpoint=url, file_path=f"/{name}/main.go")
        for url in urls
    ] + [
        EndpointReference(
            service_name=name, endpoint=var, file_path=f"/{name}/main.go", kind=ReferenceKind.ENV_VAR
        )
        for var in env_vars or []
    ]
    unscanned = (
        [UnscannedFile(path=f"/{name}/x.rs", reason=UnscannedReason.UNSUPPORTED_LANGUAGE)]
        if incomplete
        else []
    )
    return ServiceScan(
        service=ServiceBoundary(name=name, root=name, source=BoundarySource.MARKER),
        references=refs,
        coverage=ServiceCoverage(service_name=name, root=name, files_scanned=1, unscanned=unscanned),
    )


def _truth(**overrides: object) -> AppTruth:
    data: dict[str, object] = {
        "app": "demo",
        "upstream": "https://example.invalid/demo",
        "gt_source": "test",
        "repos": [{"name": "demo", "url": "https://example.invalid/demo.git", "sha": SHA}],
        "services": {
            "front": {"kind": "service"},
            "api": {"kind": "service"},
            "worker": {"kind": "service"},
            "db": {"kind": "infra"},
        },
        "edges": [
            {"from": "front", "to": "api", "proto": "http", "kind": "svc", "mechanism": "m", "source": "s"},
            {"from": "api", "to": "db", "proto": "tcp", "kind": "infra", "mechanism": "m", "source": "s"},
            {"from": "worker", "to": "api", "proto": "http", "kind": "svc", "mechanism": "m", "source": "s"},
            {"from": "front", "to": "worker", "proto": "http", "kind": "svc", "status": "ambiguous",
             "mechanism": "m", "source": "s"},
        ],
        "mapping": {"boundaries": {"tools": None, "docs": OPTIONAL}, "hosts": {"example": NOISE}},
    }
    data.update(overrides)
    return AppTruth.model_validate(data)


def _scan(*services: ServiceScan) -> RepositoryScan:
    return RepositoryScan(
        revision=ScannedRevision(repository="demo", branch="main", commit_sha=SHA),
        services=list(services),
    )


class ScoreTests(unittest.TestCase):
    def test_empty_denominators_are_none(self) -> None:
        self.assertIsNone(score(0, 0, 0).precision)
        self.assertIsNone(score(0, 0, 0).recall)
        self.assertEqual(score(1, 1, 2).precision, 0.5)
        self.assertEqual(score(1, 1, 2).recall, 0.333)

    def test_cluster_dns_reduces_to_service_name(self) -> None:
        self.assertEqual(normalize_host("http://api.shop.svc.cluster.local:8080/x"), "api")
        self.assertEqual(normalize_host("api.shop.svc:80"), "api")
        self.assertEqual(normalize_host("http://API:80"), "api")


class ScoreAppTests(unittest.TestCase):
    def setUp(self) -> None:
        scan = _scan(
            _service("front", ["http://api:8080", "http://example/docs", "http://worker"]),
            _service("api", [], env_vars=["DB_URL"]),
            _service("worker", [], incomplete=False),
            _service("tools", ["http://api"]),
            _service("docs", []),
        )
        self.result = score_app(_truth(), [("demo", scan)], {})

    def test_inventory_counts_bogus_and_skips_optional(self) -> None:
        inventory = self.result.inventory
        self.assertEqual((inventory.tp, inventory.fp, inventory.fn), (3, 1, 0))
        self.assertEqual(inventory.bogus, [{"boundary": "demo:tools", "root": "tools"}])

    def test_static_edges_ignore_noise_and_non_core_edges(self) -> None:
        static = self.result.edges[EdgeMode.STATIC]
        self.assertEqual([tp["edge"] for tp in static.true_positives], [["front", "api"]])
        # front->worker is ambiguous (not scored); tools->api comes from a bogus boundary.
        self.assertEqual(
            [fp["edge"] for fp in static.false_positives], [["BOGUS:demo:tools", "api"]]
        )
        self.assertEqual((static.all.tp, static.all.fn), (1, 2))

    def test_manifest_mode_resolves_env_references(self) -> None:
        envs = {"api": WorkloadEnv(variables={"DB_URL": "postgres://db:5432/app"})}
        scan = _scan(_service("api", [], env_vars=["DB_URL"]))
        result = score_app(_truth(), [("demo", scan)], envs)
        self.assertEqual(result.edges[EdgeMode.STATIC].infra.tp, 0)
        self.assertEqual(result.edges[EdgeMode.MANIFEST].infra.tp, 1)

    def test_workload_names_map_to_truth_names(self) -> None:
        truth = _truth(mapping={"workloads": {"api-deploy": "api"}})
        envs = {"api-deploy": WorkloadEnv(variables={"DB_URL": "postgres://db:5432/app"})}
        scan = _scan(_service("api", [], env_vars=["DB_URL"]))
        result = score_app(truth, [("demo", scan)], envs)
        self.assertEqual(result.edges[EdgeMode.MANIFEST].infra.tp, 1)

    def test_misses_split_into_flagged_silent_and_no_boundary(self) -> None:
        scan = _scan(_service("api", [], incomplete=True), _service("worker", []))
        misses = score_app(_truth(), [("demo", scan)], {}).misses
        # front->api: front has no boundary; api->db: api is incomplete;
        # worker->api: worker claims complete coverage.
        self.assertEqual(
            (misses.missed_caller_has_no_boundary, misses.missed_flagged, misses.missed_silent),
            (1, 1, 1),
        )


def _headline(silent: int = 5, precision: float | None = 0.8) -> Headline:
    return Headline(
        inventory_precision=precision,
        inventory_recall=0.9,
        static_precision=0.5,
        static_recall=0.1,
        manifest_precision=0.7,
        manifest_recall=0.3,
        missed_silent=silent,
        missed_flagged=3,
    )


class RatchetTests(unittest.TestCase):
    def report(self, **kwargs: object) -> BenchReport:
        headline = _headline(**kwargs)  # type: ignore[arg-type]
        return BenchReport(apps={"demo": headline}, pooled=headline)

    def test_unchanged_passes(self) -> None:
        self.assertEqual(regressions(self.report(), self.report()), [])

    def test_small_drop_within_tolerance_passes(self) -> None:
        self.assertEqual(regressions(self.report(), self.report(precision=0.79)), [])

    def test_silent_miss_rise_fails(self) -> None:
        problems = regressions(self.report(), self.report(silent=6))
        self.assertEqual(len(problems), 2)  # pooled and the app
        self.assertIn("silent misses rose 5 -> 6", problems[0])

    def test_precision_drop_fails(self) -> None:
        self.assertTrue(regressions(self.report(), self.report(precision=0.7)))

    def test_no_predictions_is_not_a_precision_drop(self) -> None:
        self.assertEqual(regressions(self.report(), self.report(precision=None)), [])

    def test_missing_app_fails(self) -> None:
        current = BenchReport(apps={}, pooled=_headline())
        self.assertEqual(regressions(self.report(), current), ["demo: missing from this run"])


class EnvSourceTests(unittest.TestCase):
    def test_k8s_reads_literals_and_config_maps_but_not_secrets(self) -> None:
        manifest = """
apiVersion: v1
kind: ConfigMap
metadata: {name: cfg}
data: {QUEUE_HOST: rabbit}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: api}
spec:
  template:
    spec:
      containers:
      - name: api
        envFrom: [{configMapRef: {name: cfg}}, {secretRef: {name: creds}}]
        env:
        - {name: DB_URL, value: "postgres://db:5432"}
        - {name: PASSWORD, valueFrom: {secretKeyRef: {name: creds, key: pw}}}
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "app.yaml")
            path.write_text(manifest, encoding="utf-8")
            env = k8s_envs([path])["api"]
        self.assertEqual(env.variables, {"QUEUE_HOST": "rabbit", "DB_URL": "postgres://db:5432"})
        self.assertEqual(env.opaque, ["PASSWORD"])
        self.assertTrue(env.has_opaque_sources)

    def test_compose_substitutes_dotenv_defaults(self) -> None:
        compose = "services:\n  web:\n    environment:\n      API: http://${API_HOST:-api}:${PORT}\n      TOKEN:\n"
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "compose.yaml").write_text(compose, encoding="utf-8")
            Path(tmp, ".env").write_text("PORT=8080\n", encoding="utf-8")
            env = compose_envs([Path(tmp, "compose.yaml")], Path(tmp, ".env"))["web"]
        self.assertEqual(env.variables, {"API": "http://api:8080"})
        self.assertEqual(env.opaque, ["TOKEN"])

    def test_substitute_is_bounded(self) -> None:
        self.assertEqual(substitute("${A}", {"A": "${A}"}), "${A}")


class GroundTruthIntegrityTests(unittest.TestCase):
    """The committed ground truth must stay well-formed and sourced."""

    def test_every_app_file_validates_and_edges_name_known_nodes(self) -> None:
        apps = all_apps()
        self.assertGreaterEqual(len(apps), 7)
        for app in apps:
            with self.subTest(app=app):
                truth = load_truth(app)
                self.assertEqual(truth.app, app)
                known = set(truth.services) | set(truth.observability)
                for edge in truth.edges:
                    self.assertIn(edge.caller, known)
                    self.assertIn(edge.callee, known)
                    self.assertTrue(edge.source.strip())

    def test_env_sources_name_pinned_repositories(self) -> None:
        for app in all_apps():
            truth = load_truth(app)
            repos = {pin.name for pin in truth.repos}
            for source in truth.env_sources:
                for path in [*source.paths, *([source.dotenv] if source.dotenv else [])]:
                    with self.subTest(app=app, path=path):
                        head = path.split("/", 1)[0]
                        self.assertTrue(head == "@project" or head in repos)

    def test_baseline_covers_every_app(self) -> None:
        baseline = BenchReport.model_validate_json(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(baseline.apps), all_apps())

    def test_no_app_file_embeds_local_paths(self) -> None:
        for path in APPS_DIR.glob("*.json"):
            self.assertIsNone(re.search(r"/tmp/|/root/|scratchpad", path.read_text(encoding="utf-8")), path.name)


if __name__ == "__main__":
    unittest.main()

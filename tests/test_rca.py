"""Tests for the advisory root-cause view."""

import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src import api, webhooks
from src.coordinator import nodes as coordinator_nodes
from src.core.models import (
    AssessmentEvidence,
    ContractLink,
    ContractRole,
    DependencyEdge,
    DeployedWorkload,
    DiscoveryMethod,
    KnowledgeGraph,
    ReasonCode,
    ReleaseAssessment,
    RepositoryCoverage,
    ScanContext,
    ScannedRevision,
    ServiceCoverage,
    ServiceEnvironment,
    ServiceNode,
)
from src.core.service_ids import unresolved_entity
from src.orchestration.rca import (
    BaselineView,
    ChangeKind,
    EvidenceStrength,
    PullRequestTouch,
    build_rca,
    recorded_changes,
    resolve_service,
)
from src.tools.baseline_store import BaselineScope, StoredBaseline

URN = "urn:release-coord:acme:prod:aks-1:shop"
T0 = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def _id(name: str) -> str:
    return f"{URN}:{name}"


def _edge(source: str, target: str, *methods: DiscoveryMethod, seen: datetime = T0) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=_id(source),
        target_service_id=target if target.startswith("unresolved:") else _id(target),
        discovery_methods=list(methods),
        confidence_score=1.0,
        last_observed_at=seen,
    )


def _graph(
    *,
    orders_image: str = "acr.io/orders:1",
    commit: str = "a" * 40,
    db_env: str = "postgres://db",
    contracts: tuple[str, ...] = (),
    with_cache: bool = True,
) -> KnowledgeGraph:
    services = [
        ServiceNode(service_id=_id("front"), name="front", metadata={"repository": "shop", "service_root": "src/front"}),
        ServiceNode(
            service_id=_id("orders"),
            name="orders",
            metadata={"repository": "shop", "service_root": "src/orders"},
            deployed_workloads=[DeployedWorkload(workload="Deployment/orders", images=[orders_image])],
        ),
        ServiceNode(service_id=_id("db"), name="db"),
        ServiceNode(service_id=_id("payments"), name="payments"),
    ]
    edges = [
        _edge("front", "orders", DiscoveryMethod.STATIC_CODE),
        _edge("front", "payments", DiscoveryMethod.STATIC_CODE, DiscoveryMethod.RUNTIME_TELEMETRY, seen=T1),
        _edge("orders", "db", DiscoveryMethod.RUNTIME_TELEMETRY, seen=T1),
        _edge("orders", "unresolved:api.stripe.com", DiscoveryMethod.STATIC_CODE),
    ]
    if with_cache:
        services.append(ServiceNode(service_id=_id("cache"), name="cache"))
        edges.append(_edge("orders", "cache", DiscoveryMethod.STATIC_CODE))
    return KnowledgeGraph(
        services=services,
        edges=edges,
        unresolved_entities=[unresolved_entity("api.stripe.com")],
        scan=ScanContext(
            environment="prod",
            cluster="aks-1",
            scanned_revisions=[ScannedRevision(repository="shop", branch="main", commit_sha=commit)],
        ),
        service_environments=[ServiceEnvironment(service_id=_id("orders"), variables={"DB_URL": db_env})],
        static_coverage=[
            RepositoryCoverage(
                repository="shop",
                commit_sha=commit,
                services=[
                    ServiceCoverage(
                        service_name="orders",
                        root="src/orders",
                        contracts=[ContractLink(path=path, role=ContractRole.SERVER) for path in contracts],
                    )
                ],
            )
        ],
    )


NEW = BaselineView(
    snapshot_id="snap-2",
    graph=_graph(orders_image="acr.io/orders:2", commit="b" * 40, db_env="postgres://db2", contracts=("api.proto",)),
)
OLD = BaselineView(snapshot_id="snap-1", graph=_graph(with_cache=False))


class RcaTests(unittest.TestCase):
    def test_dependencies_by_hop_ranked_by_change_then_evidence(self) -> None:
        report = build_rca(NEW.graph, "front", [NEW, OLD])
        order = [(u.name, u.hops) for u in report.upstreams]
        # orders changed, so it ranks before payments despite weaker evidence.
        self.assertEqual(order[:2], [("orders", 1), ("payments", 1)])
        self.assertEqual({name for name, hop in order if hop == 2}, {"db", "cache"})
        by_name = {u.name: u for u in report.upstreams}
        self.assertEqual(by_name["payments"].strength, EvidenceStrength.LIVE_AND_DECLARED)
        self.assertEqual(by_name["orders"].strength, EvidenceStrength.DECLARED)
        self.assertEqual(by_name["db"].strength, EvidenceStrength.LIVE)
        self.assertEqual(by_name["db"].via, ["orders"])
        self.assertEqual(by_name["db"].runtime_last_seen, T1)
        self.assertEqual(report.unresolved[0].reference, "api.stripe.com")
        self.assertEqual(report.unresolved[0].hops, 2)
        self.assertEqual(report.unresolved[0].from_services, ["orders"])
        self.assertIn("Advisory", report.advisory)

    def test_changes_between_baselines(self) -> None:
        changes = {change.kind: change for change in recorded_changes([NEW, OLD], [_id("orders")])}
        self.assertEqual(changes[ChangeKind.DEPENDENCY_ADDED].detail, "cache")
        self.assertIn("acr.io/orders:1 → acr.io/orders:2", changes[ChangeKind.IMAGE_CHANGED].detail)
        self.assertIn("aaaaaaa → bbbbbbb", changes[ChangeKind.SOURCE_REVISION_CHANGED].detail)
        self.assertIn("api.proto", changes[ChangeKind.CONTRACTS_CHANGED].detail)
        config = changes[ChangeKind.CONFIG_CHANGED].detail
        self.assertIn("DB_URL", config)
        self.assertNotIn("postgres", config)  # names only, never values
        self.assertEqual(changes[ChangeKind.IMAGE_CHANGED].snapshot_id, "snap-2")
        self.assertEqual(changes[ChangeKind.IMAGE_CHANGED].previous_snapshot_id, "snap-1")
        cache = recorded_changes([NEW, OLD], [_id("cache")])
        self.assertEqual([c.kind for c in cache], [ChangeKind.SERVICE_ADDED])

    def test_one_baseline_lists_no_changes(self) -> None:
        report = build_rca(NEW.graph, _id("front"), [NEW])
        self.assertTrue(all(not u.changes for u in report.upstreams))
        self.assertTrue(report.notes)

    def test_recent_pull_requests_touching_the_view(self) -> None:
        touches = [
            PullRequestTouch(job_id="j1", pr_id="PR-1", services=[_id("db")]),
            PullRequestTouch(job_id="j2", pr_id="PR-2", services=["elsewhere"]),
        ]
        report = build_rca(NEW.graph, "orders", [NEW], pull_requests=touches)
        self.assertEqual([(p.pr_id, p.services) for p in report.recent_pull_requests], [("PR-1", ["db"])])

    def test_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            build_rca(NEW.graph, "nope", [NEW])
        with self.assertRaises(ValueError):
            build_rca(NEW.graph, "front", [NEW], depth=9)
        graph = NEW.graph.model_copy(
            update={"services": [*NEW.graph.services, ServiceNode(service_id=f"{URN}-b:front", name="front")]}
        )
        with self.assertRaises(ValueError):
            resolve_service(graph, "front")

    def test_merged_identity_aliases_count_as_the_same_service(self) -> None:
        report = build_rca(NEW.graph, "front", [NEW, OLD], aliases={_id("db"): [_id("orders")]})
        db = next(u for u in report.upstreams if u.name == "db")
        self.assertTrue(any(c.kind is ChangeKind.IMAGE_CHANGED for c in db.changes))


class RcaApiTests(unittest.TestCase):
    def setUp(self) -> None:
        store = MagicMock()
        store.snapshot_ids.return_value = ["snap-2", "snap-1"]
        stored = {
            view.snapshot_id: StoredBaseline(
                scope=BaselineScope(tenant="acme", project_id="shop"), snapshot_id=view.snapshot_id, graph=view.graph
            )
            for view in (NEW, OLD)
        }

        def read(_scope: Any, snapshot_id: str) -> StoredBaseline:
            return stored[snapshot_id]

        store.read.side_effect = read
        assessment = ReleaseAssessment(
            approved=False, actionable=False, reason_codes=[ReasonCode.COMPATIBILITY_UNVERIFIED],
            deployment_sequence=[_id("orders")], services_to_deploy=[_id("orders")], justification="x",
            evidence=AssessmentEvidence(pr_id="PR-7", baseline_snapshot_id="snap-2"),
        )
        jobs = {
            "job-1": webhooks.PrAssessmentJob(
                job_id="job-1", pr_id="PR-7", status="completed", tenant="acme", project_id="shop", assessment=assessment
            ),
            "job-2": webhooks.PrAssessmentJob(
                job_id="job-2", pr_id="PR-8", status="completed", tenant="other", project_id="shop", assessment=assessment
            ),
        }
        for patcher in (
            patch.object(coordinator_nodes, "BASELINE_STORE", store),
            patch.object(coordinator_nodes, "review_in_force", return_value=None),
            patch.dict(webhooks._assessment_jobs, jobs, clear=True),
            patch.object(webhooks, "ASSESSMENT_ARCHIVE", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app)

    def test_report(self) -> None:
        response = self.client.get("/api/v1/projects/acme/shop/rca", params={"service": "front", "depth": 2})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["baselines_compared"], ["snap-2", "snap-1"])
        self.assertEqual(body["upstreams"][0]["name"], "orders")
        self.assertEqual([p["pr_id"] for p in body["recent_pull_requests"]], ["PR-7"])

    def test_errors(self) -> None:
        self.assertEqual(self.client.get("/api/v1/projects/acme/shop/rca", params={"service": "x"}).status_code, 404)
        self.assertEqual(
            self.client.get("/api/v1/projects/acme/shop/rca", params={"service": "front", "depth": 9}).status_code, 422
        )


if __name__ == "__main__":
    unittest.main()

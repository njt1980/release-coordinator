"""Tests for versioned baseline snapshots bridging discovery and coordination."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError

from src.coordinator import nodes as coordinator_nodes
from src.core.models import (
    UnresolvedEntityKind,
    UNRESOLVED_ID_PREFIX,
    ContractChange,
    DependencyEdge,
    DiscoveryIncompleteError,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    EvidenceIssue,
    KnowledgeGraph,
    PullRequestPayload,
    ReasonCode,
    RuntimeEvidenceState,
    ScanContext,
    ServiceNode,
    UnresolvedEntity,
)
from src.core.service_ids import unresolved_entity
from src.discovery_engine import nodes as discovery_nodes
from src.tools.baseline_store import BaselineScope, BaselineStore

COMPLETE = {DiscoveryStatus.SUCCESS, DiscoveryStatus.SKIPPED}
SCOPE = BaselineScope(tenant="acme", project_id="shop")
URN = "urn:release-coord:acme:prod:aks-1:shop"
TARGET = f"{URN}:payment-service"


def _id(node: str) -> str:
    """Return the FQURI for a service name; unresolved IDs pass through."""
    return node if node.startswith(UNRESOLVED_ID_PREFIX) else f"{URN}:{node}"


def _edge(source: str, target: str, provenance: str = "azure_devops") -> DependencyEdge:
    return DependencyEdge(
        source_service_id=_id(source),
        target_service_id=_id(target),
        discovery_methods=[DiscoveryMethod.STATIC_CODE],
        confidence_score=0.0 if provenance == "mock_fixture" else 0.7,
        last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        provenance=provenance,
    )


def _reports(
    runtime_status: DiscoveryStatus = DiscoveryStatus.SUCCESS,
) -> list[DiscoveryStageReport]:
    errors = [] if runtime_status in COMPLETE else ["query timed out"]
    return [
        DiscoveryStageReport(
            source=DiscoveryMethod.STATIC_CODE, status=DiscoveryStatus.SUCCESS
        ),
        DiscoveryStageReport(
            source=DiscoveryMethod.K8S_MANIFEST, status=DiscoveryStatus.SKIPPED
        ),
        DiscoveryStageReport(
            source=DiscoveryMethod.RUNTIME_TELEMETRY,
            status=runtime_status,
            errors=errors,
        ),
    ]


class _StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = BaselineStore(self.root)
        for module in (discovery_nodes, coordinator_nodes):
            patcher = patch.object(module, "BASELINE_STORE", self.store)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _persist(
        self,
        edges: list[DependencyEdge],
        reports: list[DiscoveryStageReport] | None = None,
        scope: BaselineScope = SCOPE,
        unresolved: list[UnresolvedEntity] | None = None,
    ) -> str:
        """Persist ``edges``, registering every non-unresolved endpoint."""
        entities = unresolved or []
        entity_ids = {entity.entity_id for entity in entities}
        service_ids = [TARGET]
        for edge in edges:
            for node in (edge.source_service_id, edge.target_service_id):
                if node not in entity_ids and node not in service_ids:
                    service_ids.append(node)
        state: Any = {
            "tenant": scope.tenant,
            "project_id": scope.project_id,
            "environment": "prod",
            "cluster": "aks-1",
            "discovery_reports": _reports() if reports is None else reports,
            "reconciled_graph": {
                "services": [
                    ServiceNode(service_id=service_id, name=service_id.rsplit(":", 1)[1])
                    for service_id in service_ids
                ],
                "edges": edges,
                "unresolved_entities": entities,
            },
        }
        result = discovery_nodes.persist_to_graph_db(state)
        snapshot_id = result["baseline_snapshot_id"]
        assert isinstance(snapshot_id, str)
        return snapshot_id

    def _snapshot_files(self, scope: BaselineScope = SCOPE) -> list[Path]:
        directory = self.root / scope.tenant / scope.project_id
        return sorted(directory.glob("*.json")) if directory.exists() else []


class BaselinePersistenceTests(_StoreTestCase):
    def test_persist_writes_versioned_snapshot_under_tenant_and_project(self) -> None:
        snapshot_id = self._persist([_edge("api-gateway", "payment-service")])

        path = self.root / "acme" / "shop" / f"{snapshot_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["services"][0]["service_id"], TARGET)
        self.assertEqual(data["edges"][0]["discovery_methods"], ["STATIC_CODE"])
        self.assertEqual(data["edges"][0]["provenance"], "azure_devops")
        self.assertEqual(
            DependencyEdge.model_validate(data["edges"][0]),
            _edge("api-gateway", "payment-service"),
        )
        self.assertEqual(
            [report["status"] for report in data["discovery"]],
            ["SUCCESS", "SKIPPED", "SUCCESS"],
        )

    def test_persist_defaults_scope_when_state_names_none(self) -> None:
        state: Any = {
            "environment": "prod",
            "cluster": "aks-1",
            "discovery_reports": _reports(),
            "reconciled_graph": {},
        }
        discovery_nodes.persist_to_graph_db(state)
        self.assertEqual(len(self._snapshot_files(BaselineScope())), 1)

    def test_each_run_writes_its_own_snapshot(self) -> None:
        first = self._persist([_edge("a", "b")])
        second = self._persist([_edge("a", "c")])

        self.assertNotEqual(first, second)
        self.assertEqual(len(self._snapshot_files()), 2)
        latest = self.store.latest(SCOPE)
        assert latest is not None
        self.assertEqual(latest.snapshot_id, max(first, second))
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_projects_are_isolated(self) -> None:
        other = BaselineScope(tenant="acme", project_id="billing")
        self._persist([_edge("a", "b")])
        self._persist([_edge("x", "y")], scope=other)

        latest = self.store.latest(other)
        assert latest is not None
        self.assertEqual(latest.graph.edges[0].source_service_id, _id("x"))
        self.assertEqual(len(self._snapshot_files()), 1)

    def test_persist_refuses_baseline_when_a_stage_is_incomplete(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])
        original = self._snapshot_files()

        for status in [DiscoveryStatus.FAILED, DiscoveryStatus.PARTIAL]:
            with self.subTest(status=status):
                with self.assertRaises(DiscoveryIncompleteError) as caught:
                    self._persist([], _reports(runtime_status=status))
                self.assertIn(f"RUNTIME_TELEMETRY {status.value}", str(caught.exception))
                self.assertIn("query timed out", str(caught.exception))
                # No snapshot is written, so the good baseline stays latest.
                self.assertEqual(self._snapshot_files(), original)

    def test_persist_refuses_baseline_when_a_stage_did_not_report(self) -> None:
        with self.assertRaises(DiscoveryIncompleteError):
            self._persist([], _reports()[:2])
        self.assertEqual(self._snapshot_files(), [])

    def test_persist_refuses_baseline_when_every_stage_skipped(self) -> None:
        skipped = [
            report.model_copy(update={"status": DiscoveryStatus.SKIPPED})
            for report in _reports()
        ]
        with self.assertRaises(DiscoveryIncompleteError):
            self._persist([], skipped)

    def test_store_refuses_incomplete_graph_directly(self) -> None:
        with self.assertRaises(DiscoveryIncompleteError):
            self.store.write(SCOPE, KnowledgeGraph(services=[], edges=[]))
        self.assertEqual(self._snapshot_files(), [])

    def test_latest_skips_newer_unusable_snapshots(self) -> None:
        good = self._persist([_edge("a", "b")])
        directory = self.root / "acme" / "shop"
        incomplete = {
            "services": [],
            "edges": [],
            "discovery": [
                r.model_dump(mode="json")
                for r in _reports(runtime_status=DiscoveryStatus.FAILED)
            ],
        }
        (directory / "99990101T000000000000Z-incompl.json").write_text(
            json.dumps(incomplete), encoding="utf-8"
        )
        (directory / "99990101T000000000000Z-legacy0.json").write_text(
            json.dumps({"services": [], "edges": []}), encoding="utf-8"
        )
        (directory / "99990101T000000000000Z-garbage.json").write_text(
            "{not json", encoding="utf-8"
        )
        complete = [r.model_dump(mode="json") for r in _reports()]
        pre_fquri = {
            "services": [{"service_id": "shop/a", "name": "a"}],
            "edges": [],
            "discovery": complete,
        }
        (directory / "99990101T000000000000Z-prefqur.json").write_text(
            json.dumps(pre_fquri), encoding="utf-8"
        )
        other_tenant = {
            "services": [
                {"service_id": "urn:release-coord:evil:unknown:unknown:shop:a", "name": "a"}
            ],
            "edges": [],
            "discovery": complete,
        }
        (directory / "99990101T000000000000Z-tenant0.json").write_text(
            json.dumps(other_tenant), encoding="utf-8"
        )

        with self.assertLogs("src.tools.baseline_store", level="WARNING") as logs:
            latest = self.store.latest(SCOPE)

        assert latest is not None
        self.assertEqual(latest.snapshot_id, good)
        self.assertEqual(len(logs.records), 5)

    def test_store_refuses_services_from_another_tenant(self) -> None:
        graph = KnowledgeGraph(
            services=[ServiceNode(service_id=TARGET, name="payment-service")],
            edges=[],
            discovery=_reports(),
            scan=ScanContext(environment="prod", cluster="aks-1"),
        )
        with self.assertRaises(ValueError):
            self.store.write(BaselineScope(tenant="other", project_id="shop"), graph)
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_latest_ignores_in_progress_temp_files(self) -> None:
        directory = self.root / "acme" / "shop"
        directory.mkdir(parents=True)
        (directory / ".20260101T000000000000Z-abc.x.tmp").write_text("{}")
        self.assertIsNone(self.store.latest(SCOPE))

    def test_scope_and_snapshot_ids_reject_path_traversal(self) -> None:
        for tenant in ["..", "../x", "a/b", ".hidden", ""]:
            with self.subTest(tenant=tenant):
                with self.assertRaises(ValidationError):
                    BaselineScope(tenant=tenant)
        with self.assertRaises(ValueError):
            self.store.read(SCOPE, "../escape")

    def test_demo_mode_reflects_mock_fixture_edges(self) -> None:
        self._persist([_edge("a", "b")])
        latest = self.store.latest(SCOPE)
        assert latest is not None
        self.assertFalse(latest.demo_mode)

        self._persist([_edge("a", "b"), _edge("c", "d", provenance="mock_fixture")])
        latest = self.store.latest(SCOPE)
        assert latest is not None
        self.assertTrue(latest.demo_mode)


class StaticRadiusTests(_StoreTestCase):
    def _radius(
        self, scope: BaselineScope = SCOPE, target: str = TARGET
    ) -> dict[str, object]:
        state: Any = {
            "tenant": scope.tenant,
            "project_id": scope.project_id,
            "target_services": [target],
        }
        return coordinator_nodes.calculate_static_radius_node(state)

    def test_static_radius_splits_upstream_and_downstream(self) -> None:
        snapshot_id = self._persist(
            [
                _edge("api-gateway", "payment-service"),
                _edge("payment-service", "ledger-service"),
                _edge("order-service", "payment-service"),
                _edge("order-service", "inventory-service"),
            ]
        )

        result = self._radius()

        self.assertEqual(
            result["upstream_services"], [_id("api-gateway"), _id("order-service")]
        )
        self.assertEqual(result["downstream_services"], [_id("ledger-service")])
        matched = result["matched_edges"]
        assert isinstance(matched, list)
        self.assertEqual(len(matched), 3)
        self.assertEqual(result["discovery_issues"], [])
        self.assertEqual(result["baseline_snapshot_id"], snapshot_id)
        self.assertFalse(result["baseline_demo_mode"])

    def test_static_radius_is_fully_transitive(self) -> None:
        self._persist(
            [
                _edge("web", "api-gateway"),
                _edge("api-gateway", "checkout"),
                _edge("checkout", "payment-service"),
                _edge("payment-service", "ledger-service"),
                _edge("ledger-service", "postgres"),
                _edge("postgres", "backup-agent"),
                _edge("reporting", "inventory"),  # not connected to the target
            ]
        )

        result = self._radius()

        self.assertEqual(
            result["upstream_services"],
            [_id("checkout"), _id("api-gateway"), _id("web")],
        )
        self.assertEqual(
            result["downstream_services"],
            [_id("ledger-service"), _id("postgres"), _id("backup-agent")],
        )
        matched = result["matched_edges"]
        assert isinstance(matched, list)
        self.assertEqual(len(matched), 6)
        self.assertNotIn(_id("reporting"), [e.source_service_id for e in matched])
        self.assertEqual(result["discovery_issues"], [])

    def test_static_radius_terminates_on_cycles(self) -> None:
        self._persist(
            [
                _edge("payment-service", "ledger-service"),
                _edge("ledger-service", "fraud-check"),
                _edge("fraud-check", "payment-service"),  # cycle through target
                _edge("fraud-check", "ledger-service"),  # inner cycle
                _edge("x", "y"),
                _edge("y", "x"),  # disconnected cycle
            ]
        )

        result = self._radius()

        self.assertEqual(
            result["downstream_services"], [_id("ledger-service"), _id("fraud-check")]
        )
        self.assertEqual(
            result["upstream_services"], [_id("fraud-check"), _id("ledger-service")]
        )
        matched = result["matched_edges"]
        assert isinstance(matched, list)
        keys = [(e.source_service_id, e.target_service_id) for e in matched]
        self.assertEqual(len(keys), 4)
        self.assertEqual(len(set(keys)), 4)
        self.assertEqual(result["discovery_issues"], [])

    def test_unresolved_entity_deep_downstream_is_a_discovery_issue(self) -> None:
        database = unresolved_entity("10.0.0.12:5432")
        self._persist(
            [
                _edge("payment-service", "ledger-service"),
                _edge("ledger-service", database.entity_id),
            ],
            unresolved=[database],
        )

        result = self._radius()

        self.assertEqual(result["downstream_services"], [_id("ledger-service")])
        (issue,) = result["discovery_issues"]
        self.assertEqual(
            (issue.code, issue.detail),
            (
                ReasonCode.DISCOVERY_GAP,
                "Unresolved dependency in critical path: "
                "unresolved:10.0.0.12:5432 (IP_ADDRESS) is downstream of "
                f"the release via {TARGET} -> {_id('ledger-service')} -> "
                "unresolved:10.0.0.12:5432",
            ),
        )
        # Structured for grouping: what, which kind, the cause key and the path.
        self.assertEqual([s.id for s in issue.subjects], [database.entity_id])
        self.assertIs(issue.entity_kind, UnresolvedEntityKind.IP_ADDRESS)
        self.assertEqual(issue.root_key, "host:10.0.0.12")
        self.assertEqual(issue.via, [TARGET, _id("ledger-service"), database.entity_id])

    def test_unresolved_entity_upstream_is_a_discovery_issue(self) -> None:
        caller = unresolved_entity("legacy-batch")
        self._persist(
            [
                _edge(caller.entity_id, "checkout"),
                _edge("checkout", "payment-service"),
            ],
            unresolved=[caller],
        )

        issues = self._radius()["discovery_issues"]

        assert isinstance(issues, list)
        self.assertEqual(len(issues), 1)
        self.assertIs(issues[0].code, ReasonCode.DISCOVERY_GAP)
        self.assertIn("unresolved:legacy-batch (UNREGISTERED_SERVICE)", issues[0].detail)
        self.assertIn(
            f"upstream of the release via unresolved:legacy-batch -> "
            f"{_id('checkout')} -> {TARGET}",
            issues[0].detail,
        )

    def test_unresolved_entity_outside_radius_is_ignored(self) -> None:
        external = unresolved_entity("api.stripe.com")
        self._persist(
            [
                _edge("payment-service", "ledger-service"),
                _edge("reporting", external.entity_id),
            ],
            unresolved=[external],
        )
        self.assertEqual(self._radius()["discovery_issues"], [])

    def test_unresolved_dependency_rejects_release_without_llm(self) -> None:
        external = unresolved_entity("vault.corp.internal")
        self._persist(
            [
                _edge("checkout", "payment-service"),
                _edge("payment-service", external.entity_id),
            ],
            unresolved=[external],
        )
        pull_request = PullRequestPayload(
            pr_id="PR-9",
            title="t",
            target_service=TARGET,
            change_type=ContractChange.ADDITIVE,
            diff="d",
        )
        state: Any = {
            "tenant": "acme",
            "project_id": "shop",
            "workspace_id": "",
            "pull_request": pull_request,
            "target_services": [TARGET],
        }
        state.update(coordinator_nodes.calculate_static_radius_node(state))
        state.update(coordinator_nodes.validate_runtime_telemetry_node(state))
        state.update(coordinator_nodes.sequence_deployment_node(state))
        with patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls:
            state.update(coordinator_nodes.assess_risk_and_sequence_node(state))
        reasoner_cls.assert_not_called()

        assessment = coordinator_nodes.compile_evidence_node(state)["release_assessment"]

        self.assertFalse(assessment.approved)
        self.assertFalse(assessment.actionable)
        self.assertIsNone(assessment.risk_score)
        self.assertIn(ReasonCode.DISCOVERY_GAP, assessment.reason_codes)
        self.assertIn(
            "Unresolved dependency in critical path: unresolved:vault.corp.internal "
            "(PRIVATE_DNS)",
            assessment.justification,
        )
        self.assertEqual(assessment.deployment_sequence, [TARGET, _id("checkout")])

    def test_target_with_unknown_segments_resolves_to_registered_fquri(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])

        result = self._radius(
            target="urn:release-coord:acme:unknown:unknown:unknown:payment-service"
        )

        self.assertEqual(result["target_services"], [TARGET])
        self.assertEqual(result["upstream_services"], [_id("api-gateway")])
        self.assertEqual(result["discovery_issues"], [])

    def test_unregistered_target_is_a_discovery_issue(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])

        ghost = f"{URN}:ghost-service"
        result = self._radius(target=ghost)

        self.assertEqual(result["target_services"], [ghost])
        self.assertEqual(
            result["discovery_issues"],
            [
                EvidenceIssue(
                    code=ReasonCode.DISCOVERY_GAP,
                    detail=(
                        f"Unresolved dependency in critical path: target {ghost} "
                        "does not match exactly one registered service in the "
                        "baseline"
                    ),
                )
            ],
        )

    def test_static_radius_uses_latest_good_snapshot_after_failed_run(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])
        with self.assertRaises(DiscoveryIncompleteError):
            self._persist([], _reports(runtime_status=DiscoveryStatus.FAILED))

        result = self._radius()

        self.assertEqual(result["upstream_services"], [_id("api-gateway")])
        self.assertEqual(result["discovery_issues"], [])

    def test_static_radius_reports_demo_mode(self) -> None:
        self._persist([_edge("api-gateway", "payment-service", "mock_fixture")])

        result = self._radius()

        self.assertTrue(result["baseline_demo_mode"])
        issues = result["discovery_issues"]
        assert isinstance(issues, list)
        self.assertEqual([issue.code for issue in issues], [ReasonCode.DEMO_EVIDENCE])

    def test_pinned_snapshot_is_used_even_when_newer_exists(self) -> None:
        pinned = self._persist([_edge("api-gateway", "payment-service")])
        self._persist([_edge("checkout", "payment-service")])

        state: Any = {
            "tenant": SCOPE.tenant,
            "project_id": SCOPE.project_id,
            "target_services": [TARGET],
            "baseline_snapshot_id": pinned,
        }
        result = coordinator_nodes.calculate_static_radius_node(state)

        self.assertEqual(result["baseline_snapshot_id"], pinned)
        self.assertEqual(result["upstream_services"], [_id("api-gateway")])

    def test_unusable_pinned_snapshot_never_falls_back_to_latest(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])

        state: Any = {
            "tenant": SCOPE.tenant,
            "project_id": SCOPE.project_id,
            "target_services": [TARGET],
            "baseline_snapshot_id": "20200101T000000000000Z-deadbeef",
        }
        with self.assertLogs(coordinator_nodes.logger, level="WARNING"):
            result = coordinator_nodes.calculate_static_radius_node(state)

        self.assertEqual(result["upstream_services"], [])
        issues = result["discovery_issues"]
        assert isinstance(issues, list)
        self.assertEqual(
            [issue.code for issue in issues], [ReasonCode.BASELINE_UNAVAILABLE]
        )
        self.assertIn("20200101T000000000000Z-deadbeef", issues[0].detail)

    def test_static_radius_missing_baseline_is_a_discovery_issue(self) -> None:
        self._persist([_edge("api-gateway", "payment-service")])
        other = BaselineScope(tenant="acme", project_id="unknown-project")

        with self.assertLogs(coordinator_nodes.logger, level="WARNING"):
            result = self._radius(other)

        self.assertEqual(
            result,
            {
                "upstream_services": [],
                "downstream_services": [],
                "matched_edges": [],
                "baseline_runtime_state": RuntimeEvidenceState.NONE,
                "baseline_graph": None,
                "discovery_issues": [
                    EvidenceIssue(
                        code=ReasonCode.BASELINE_UNAVAILABLE,
                        detail=(
                            "No complete discovery baseline found for "
                            "acme/unknown-project; run discovery first."
                        ),
                    )
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()

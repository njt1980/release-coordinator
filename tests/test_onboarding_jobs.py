"""Tests for in-memory discovery job tracking and the status endpoint."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

from src import api
from src.api import CrawlResults, JobRecord, OnboardingPayload
from src.core.models import (
    DependencyEdge,
    DiscoveryIncompleteError,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    KnowledgeGraph,
    ServiceNode,
)
from src.tools.baseline_store import BaselineScope, BaselineStore


REPORTS = [
    DiscoveryStageReport(source=method, status=status)
    for method, status in [
        (DiscoveryMethod.STATIC_CODE, DiscoveryStatus.SKIPPED),
        (DiscoveryMethod.K8S_MANIFEST, DiscoveryStatus.SUCCESS),
        (DiscoveryMethod.RUNTIME_TELEMETRY, DiscoveryStatus.SKIPPED),
    ]
]


class OnboardingJobTests(unittest.TestCase):
    def setUp(self) -> None:
        jobs_patch = patch.dict(api._jobs, clear=True)
        jobs_patch.start()
        self.addCleanup(jobs_patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = BaselineStore(self.root)
        store_patch = patch.object(api, "BASELINE_STORE", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.scope = BaselineScope(tenant="acme", project_id="shop")

    def _write_raw(self, snapshot_id: str, content: str) -> None:
        directory = self.store.project_dir(self.scope)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{snapshot_id}.json").write_text(content, encoding="utf-8")

    def _invoke_returning(self, snapshot_id: str) -> MagicMock:
        return MagicMock(
            invoke=MagicMock(return_value={"baseline_snapshot_id": snapshot_id})
        )

    def _state(self) -> api.DiscoveryGraphState:
        return api.build_discovery_state(
            OnboardingPayload(
                tenant="acme", project_id="shop", target_namespaces=["payments"]
            )
        )

    def test_onboard_registers_queued_job_and_schedules_wrapper(self) -> None:
        tasks = BackgroundTasks()
        response = api.onboard(OnboardingPayload(workspace_id="ws-1"), tasks)

        self.assertEqual(
            api._jobs[response.job_id],
            JobRecord(job_id=response.job_id, status="queued"),
        )
        self.assertEqual(len(tasks.tasks), 1)
        self.assertIs(tasks.tasks[0].func, api.run_discovery_job)
        self.assertEqual(tasks.tasks[0].args[0], response.job_id)

    def test_wrapper_marks_running_then_completed_with_results(self) -> None:
        observed: list[str] = []

        services = [ServiceNode(service_id=name, name=name) for name in "abc"]
        edges = [
            DependencyEdge(
                source_service_id="a",
                target_service_id="b",
                discovery_methods=[DiscoveryMethod.STATIC_CODE],
                confidence_score=0.5,
                last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ]

        snapshot_ids: list[str] = []

        def fake_invoke(state: api.DiscoveryGraphState) -> dict[str, object]:
            observed.append(api._jobs["job-1"].status)
            scope = BaselineScope(
                tenant=state["tenant"], project_id=state["project_id"]
            )
            graph = KnowledgeGraph(services=services, edges=edges, discovery=REPORTS)
            stored = self.store.write(scope, graph)
            snapshot_ids.append(stored.snapshot_id)
            # A concurrent run for the same project lands a newer snapshot.
            self.store.write(scope, graph.model_copy(update={"services": []}))
            return {"baseline_snapshot_id": stored.snapshot_id}

        with patch.object(api, "discovery_app", MagicMock(invoke=fake_invoke)):
            asyncio.run(api.run_discovery_job("job-1", self._state()))

        self.assertEqual(observed, ["running"])
        self.assertEqual(
            api._jobs["job-1"],
            JobRecord(
                job_id="job-1",
                status="completed",
                results=CrawlResults(
                    tenant="acme",
                    project_id="shop",
                    snapshot_id=snapshot_ids[0],
                    demo_mode=False,
                    service_count=3,
                    edge_count=1,
                    services=services,
                    edges=edges,
                    discovery=REPORTS,
                ),
            ),
        )

    def test_wrapper_fails_when_discovery_refuses_baseline(self) -> None:
        error = DiscoveryIncompleteError(
            ["RUNTIME_TELEMETRY FAILED: query timed out"]
        )
        failing = MagicMock(invoke=MagicMock(side_effect=error))
        with patch.object(api, "discovery_app", failing):
            asyncio.run(api.run_discovery_job("job-7", self._state()))

        record = api._jobs["job-7"]
        self.assertEqual(record.status, "failed")
        self.assertIsNone(record.results)
        assert record.error_message is not None
        self.assertIn("Baseline not established", record.error_message)
        self.assertIn("RUNTIME_TELEMETRY FAILED", record.error_message)

    def test_wrapper_fails_when_graph_lacks_complete_discovery(self) -> None:
        incomplete = [
            REPORTS[0],
            REPORTS[1].model_copy(
                update={"status": DiscoveryStatus.PARTIAL, "errors": ["ns-b: 403"]}
            ),
            REPORTS[2],
        ]
        for job_id, discovery in [("job-8", None), ("job-9", incomplete)]:
            with self.subTest(job_id=job_id):
                graph = KnowledgeGraph(services=[], edges=[], discovery=discovery)
                self._write_raw(job_id, graph.model_dump_json())
                with patch.object(api, "discovery_app", self._invoke_returning(job_id)):
                    asyncio.run(api.run_discovery_job(job_id, self._state()))
                self.assertEqual(api._jobs[job_id].status, "failed")
                self.assertIsNone(api._jobs[job_id].results)

    def test_wrapper_fails_when_graph_entries_invalid(self) -> None:
        self._write_raw("job-6", '{"services": [{"id": "a"}], "edges": []}')
        with patch.object(api, "discovery_app", self._invoke_returning("job-6")):
            asyncio.run(api.run_discovery_job("job-6", self._state()))

        self.assertEqual(api._jobs["job-6"].status, "failed")
        self.assertIsNone(api._jobs["job-6"].results)

    def test_wrapper_fails_when_snapshot_missing(self) -> None:
        with patch.object(api, "discovery_app", self._invoke_returning("job-4")):
            asyncio.run(api.run_discovery_job("job-4", self._state()))

        record = api._jobs["job-4"]
        self.assertEqual(record.status, "failed")
        self.assertIsNone(record.results)
        self.assertTrue(record.error_message)

    def test_wrapper_fails_when_graph_file_malformed(self) -> None:
        self._write_raw("job-5", '{"services": []}')
        with patch.object(api, "discovery_app", self._invoke_returning("job-5")):
            asyncio.run(api.run_discovery_job("job-5", self._state()))

        self.assertEqual(api._jobs["job-5"].status, "failed")
        self.assertIsNone(api._jobs["job-5"].results)

    def test_wrapper_fails_when_run_reports_no_snapshot(self) -> None:
        with patch.object(api, "discovery_app", MagicMock(invoke=lambda s: {})):
            asyncio.run(api.run_discovery_job("job-10", self._state()))

        record = api._jobs["job-10"]
        self.assertEqual(record.status, "failed")
        assert record.error_message is not None
        self.assertIn("without writing a baseline snapshot", record.error_message)

    def test_latest_baseline_endpoint_exposes_demo_mode(self) -> None:
        mock_edge = DependencyEdge(
            source_service_id="a",
            target_service_id="b",
            discovery_methods=[DiscoveryMethod.RUNTIME_TELEMETRY],
            confidence_score=0.0,
            last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            provenance="mock_fixture",
            environment="mock",
        )
        stored = self.store.write(
            self.scope,
            KnowledgeGraph(services=[], edges=[mock_edge], discovery=REPORTS),
        )

        results = api.get_latest_baseline("acme", "shop")

        self.assertTrue(results.demo_mode)
        self.assertEqual(results.snapshot_id, stored.snapshot_id)

    def test_latest_baseline_endpoint_errors(self) -> None:
        for tenant, project_id, code in [("acme", "none", 404), ("..", "shop", 422)]:
            with self.subTest(tenant=tenant, project_id=project_id):
                with self.assertRaises(HTTPException) as caught:
                    api.get_latest_baseline(tenant, project_id)
                self.assertEqual(caught.exception.status_code, code)

    def test_wrapper_records_failure_message(self) -> None:
        failing = MagicMock(invoke=MagicMock(side_effect=RuntimeError("boom")))
        with patch.object(api, "discovery_app", failing):
            asyncio.run(api.run_discovery_job("job-2", self._state()))

        self.assertEqual(
            api._jobs["job-2"],
            JobRecord(job_id="job-2", status="failed", error_message="boom"),
        )

    def test_status_endpoint_returns_record(self) -> None:
        record = JobRecord(job_id="job-3", status="running")
        api._jobs["job-3"] = record
        self.assertEqual(api.get_onboarding_job("job-3"), record)

    def test_status_endpoint_unknown_job_is_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            api.get_onboarding_job("missing")
        self.assertEqual(caught.exception.status_code, 404)

    def test_status_route_is_registered(self) -> None:
        routes = {
            (route.path, method)
            for route in api.app.routes
            for method in getattr(route, "methods", set())
        }
        self.assertIn(("/api/onboard/{job_id}", "GET"), routes)
        self.assertIn(("/api/baselines/{tenant}/{project_id}/latest", "GET"), routes)


if __name__ == "__main__":
    unittest.main()

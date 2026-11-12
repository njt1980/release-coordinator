"""Tests for in-memory discovery job tracking and the status endpoint."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

from src import api
from src.api import CrawlResults, JobRecord, OnboardingPayload
from src.discovery_engine import nodes as discovery_nodes
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    ServiceNode,
)


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
        self.graph_path = Path(tmp.name) / "knowledge_graph.json"
        path_patch = patch.object(api, "KNOWLEDGE_GRAPH_PATH", self.graph_path)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def _write_graph(self, graph: object) -> None:
        self.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    def _state(self) -> api.DiscoveryGraphState:
        return api.build_discovery_state(
            OnboardingPayload(target_namespaces=["payments"])
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

        def fake_invoke(state: api.DiscoveryGraphState) -> dict[str, object]:
            observed.append(api._jobs["job-1"].status)
            self._write_graph(
                {
                    "services": [s.model_dump(mode="json") for s in services],
                    "edges": [e.model_dump(mode="json") for e in edges],
                    "discovery": [r.model_dump(mode="json") for r in REPORTS],
                }
            )
            return {}

        with patch.object(api, "discovery_app", MagicMock(invoke=fake_invoke)):
            asyncio.run(api.run_discovery_job("job-1", self._state()))

        self.assertEqual(observed, ["running"])
        self.assertEqual(
            api._jobs["job-1"],
            JobRecord(
                job_id="job-1",
                status="completed",
                results=CrawlResults(
                    service_count=3,
                    edge_count=1,
                    services=services,
                    edges=edges,
                    discovery=REPORTS,
                ),
            ),
        )

    def test_wrapper_fails_when_discovery_refuses_baseline(self) -> None:
        error = discovery_nodes.DiscoveryIncompleteError(
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
                graph: dict[str, object] = {"services": [], "edges": []}
                if discovery is not None:
                    graph["discovery"] = [r.model_dump(mode="json") for r in discovery]
                self._write_graph(graph)
                with patch.object(api, "discovery_app", MagicMock()):
                    asyncio.run(api.run_discovery_job(job_id, self._state()))
                self.assertEqual(api._jobs[job_id].status, "failed")
                self.assertIsNone(api._jobs[job_id].results)

    def test_wrapper_fails_when_graph_entries_invalid(self) -> None:
        self._write_graph({"services": [{"id": "a"}], "edges": []})
        with patch.object(api, "discovery_app", MagicMock()):
            asyncio.run(api.run_discovery_job("job-6", self._state()))

        self.assertEqual(api._jobs["job-6"].status, "failed")
        self.assertIsNone(api._jobs["job-6"].results)

    def test_wrapper_fails_when_graph_file_missing(self) -> None:
        with patch.object(api, "discovery_app", MagicMock()):
            asyncio.run(api.run_discovery_job("job-4", self._state()))

        record = api._jobs["job-4"]
        self.assertEqual(record.status, "failed")
        self.assertIsNone(record.results)
        self.assertTrue(record.error_message)

    def test_wrapper_fails_when_graph_file_malformed(self) -> None:
        self._write_graph({"services": []})
        with patch.object(api, "discovery_app", MagicMock()):
            asyncio.run(api.run_discovery_job("job-5", self._state()))

        self.assertEqual(api._jobs["job-5"].status, "failed")
        self.assertIsNone(api._jobs["job-5"].results)

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


if __name__ == "__main__":
    unittest.main()

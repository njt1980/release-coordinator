"""Tests for in-memory discovery job tracking and the status endpoint."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

from src import api
from src.api import JobRecord, OnboardingPayload


class OnboardingJobTests(unittest.TestCase):
    def setUp(self) -> None:
        jobs_patch = patch.dict(api._jobs, clear=True)
        jobs_patch.start()
        self.addCleanup(jobs_patch.stop)

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

    def test_wrapper_marks_running_then_completed(self) -> None:
        observed: list[str] = []

        def fake_invoke(state: api.DiscoveryGraphState) -> dict[str, object]:
            observed.append(api._jobs["job-1"].status)
            return {}

        with patch.object(api, "discovery_app", MagicMock(invoke=fake_invoke)):
            asyncio.run(api.run_discovery_job("job-1", self._state()))

        self.assertEqual(observed, ["running"])
        self.assertEqual(
            api._jobs["job-1"], JobRecord(job_id="job-1", status="completed")
        )

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

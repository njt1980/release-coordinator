"""Tests for the decision-record export (T3 close-out): JSON and print view."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src import api, decision_api, webhooks
from src.coordinator import nodes as coordinator_nodes
from src.core.decision import assessment_digest
from src.orchestration.record_export import RecordExport, export_digest
from src.tools.decision_store import DecisionStore
from src.webhooks import PrAssessmentJob
from tests.test_decision_record import _assessment

RECORD = "/api/v1/webhooks/pr-assessment/job-1/record"


class RecordExportTests(unittest.TestCase):
    URL = "/api/v1/webhooks/pr-assessment/job-1/decisions"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        jobs = {
            "job-1": PrAssessmentJob(
                job_id="job-1", pr_id="PR-7", status="completed", tenant="acme", project_id="shop",
                assessment=_assessment(),
            ),
            "job-2": PrAssessmentJob(job_id="job-2", pr_id="PR-8", status="running"),
        }
        for patcher in (
            patch.object(decision_api, "DECISION_STORE", DecisionStore(Path(directory.name) / "d.sqlite3", vfs="")),
            patch.dict(webhooks._assessment_jobs, jobs, clear=True),
            patch.object(webhooks, "ASSESSMENT_ARCHIVE", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app)
        store = MagicMock()
        store.read.side_effect = FileNotFoundError("gone")
        patcher = patch.object(coordinator_nodes, "BASELINE_STORE", store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.baselines = store

    def _decide(self) -> None:
        self.client.post(self.URL, json={"verdict": "further_analysis", "reason": "<b>check</b> queue", "signer_name": "Sam"})
        self.client.post(self.URL, json={"verdict": "decline", "reason": "Consumer missing.", "signer_name": "Sam"})

    def test_json_export_is_self_contained_and_sealed(self) -> None:
        self._decide()
        response = self.client.get(RECORD)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('attachment; filename="decision-record-PR-7-job-1.json"', response.headers["content-disposition"])
        export = RecordExport.model_validate_json(response.text)
        self.assertEqual(export.assessment_sha256, assessment_digest(_assessment().model_dump(mode="json")))
        self.assertEqual([d.superseded for d in export.decisions], [True, False])
        self.assertTrue(all(d.record.assessment_sha256 == export.assessment_sha256 for d in export.decisions))
        self.assertTrue(export.chain.intact)
        self.assertEqual(export.chain.records, 2)
        self.assertFalse(export.baseline_snapshot_available)
        self.assertEqual(export.briefing.pr_id, "PR-7")
        # The seal covers the content: any change shows.
        self.assertEqual(export.export_sha256, export_digest(export))
        changed = json.loads(response.text)
        changed["assessment"]["justification"] = "edited"
        self.assertNotEqual(export_digest(RecordExport.model_validate(changed)), export.export_sha256)

    def test_print_view_escapes_and_shows_signatures(self) -> None:
        self.baselines.read.side_effect = None
        self._decide()
        response = self.client.get(RECORD, params={"format": "html"})
        self.assertEqual(response.status_code, 200)
        page = response.text
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Declined for release: Sam (typed name, not an electronic signature)", page)
        self.assertIn("&lt;b&gt;check&lt;/b&gt; queue", page)
        self.assertNotIn("<b>check</b>", page)
        self.assertNotIn("<script", page)
        self.assertIn("superseded by a later decision", page)
        self.assertIn("Baseline still readable</th><td>yes", page)

    def test_unknown_or_unfinished_assessments(self) -> None:
        self.assertEqual(self.client.get("/api/v1/webhooks/pr-assessment/nope/record").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/webhooks/pr-assessment/job-2/record").status_code, 409)
        self.assertEqual(self.client.get(RECORD, params={"format": "pdf"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()

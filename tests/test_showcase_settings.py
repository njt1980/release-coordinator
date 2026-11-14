"""Deployment settings: org allowlist, raw webhook switch, assessment archive, health."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

from src import api, webhooks
from src.core import config
from src.core.models import AssessmentEvidence, ReasonCode, ReleaseAssessment
from src.tools import devops_extractor
from src.tools.assessment_archive import AssessmentArchive
from src.tools.devops_extractor import DevOpsExtractor
from tests.test_pr_webhook import _payload


class AllowlistTests(unittest.TestCase):
    def test_only_allowed_organizations(self) -> None:
        allowed = frozenset({"https://dev.azure.com/acme"})
        with patch.object(config, "ADO_ORG_ALLOWLIST", allowed), patch.object(devops_extractor, "DefaultAzureCredential"):
            DevOpsExtractor(org_url="https://dev.azure.com/ACME/", project="p", repositories=[])
            with self.assertRaisesRegex(ValueError, "may read"):
                DevOpsExtractor(org_url="https://dev.azure.com/other", project="p", repositories=[])

    def test_no_allowlist_allows_any(self) -> None:
        with patch.object(config, "ADO_ORG_ALLOWLIST", frozenset()), patch.object(devops_extractor, "DefaultAzureCredential"):
            DevOpsExtractor(org_url="https://dev.azure.com/other", project="p", repositories=[])


class RawWebhookTests(unittest.TestCase):
    def test_disabled_raw_webhook_refuses(self) -> None:
        with patch.object(config, "DISABLE_RAW_WEBHOOK", True), self.assertRaises(HTTPException) as raised:
            webhooks.pr_assessment_webhook(_payload(), BackgroundTasks())
        self.assertEqual(raised.exception.status_code, 403)


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.archive = AssessmentArchive(Path(directory.name) / "assessments")

    def test_save_load_and_unsafe_ids(self) -> None:
        self.assertEqual(self.archive.load_all(), [])
        self.archive.save("job-1", "{}")
        self.archive.save("job-1", '{"x": 1}')
        self.assertEqual(self.archive.load_all(), [("job-1", '{"x": 1}')])
        with self.assertRaises(ValueError):
            self.archive.save("../escape", "{}")

    def test_finished_assessments_survive_a_restart(self) -> None:
        record = webhooks.PrAssessmentJob(
            job_id="job-9",
            pr_id="PR-9",
            status="completed",
            assessment=ReleaseAssessment(
                risk_score=0.8, approved=False, actionable=True, reason_codes=[ReasonCode.MODEL_REJECTED],
                deployment_sequence=["svc"], justification="x", evidence=AssessmentEvidence(pr_id="PR-9"),
            ),
        )
        self.archive.save("bad", "not json")
        with patch.object(webhooks, "ASSESSMENT_ARCHIVE", self.archive), patch.dict(webhooks._assessment_jobs, clear=True):
            webhooks._finish(record)
            webhooks._assessment_jobs.clear()  # A new process.
            with patch.object(webhooks, "_archive_restored", False):
                self.assertEqual([s.job_id for s in webhooks.list_pr_assessments()], ["job-9"])
                self.assertEqual(webhooks.get_pr_assessment("job-9"), record)


class HealthTests(unittest.TestCase):
    def test_healthz(self) -> None:
        self.assertEqual(TestClient(api.app).get("/healthz").json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()

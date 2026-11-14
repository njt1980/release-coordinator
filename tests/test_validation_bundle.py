"""Tests for the per-release validation-support package (JSON bundle and print view)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src import api, decision_api, webhooks
from src.coordinator import nodes as coordinator_nodes
from src.core.models import AssessmentReview, ReviewNote
from src.core.validation import ToolVersion
from src.orchestration.validation_bundle import ValidationBundle, bundle_digest
from src.tools import validation_sources
from src.tools.baseline_store import BaselineScope
from src.tools.decision_store import DecisionStore
from src.tools.review_store import ReviewStore
from src.webhooks import PrAssessmentJob
from tests.test_decision_record import _assessment
from tests.test_review import T0, _answer

BUNDLE = "/api/v1/webhooks/pr-assessment/job-1/validation-bundle"
SCOPE = BaselineScope(tenant="acme", project_id="shop")


def _reviewed_assessment():  # type: ignore[no-untyped-def]
    assessment = _assessment()
    note = ReviewNote(answer_id="a1", subject="dep", verdict="agree", reviewer="Dana", answered_at=T0)
    gone = ReviewNote(answer_id="a-gone", subject="q", verdict="answer", reviewer="Dana", answered_at=T0)
    evidence = assessment.evidence.model_copy(
        update={"review": AssessmentReview(applied=[note], needs_rereview=[gone, note])}
    )
    return assessment.model_copy(update={"evidence": evidence})


def _write_sources(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "intended-use.md").write_text("# Intended use\n\nAdvisory <only>.\n", encoding="utf-8")
    bench = root / "tests" / "benchmarks" / "discovery"
    (bench / "apps").mkdir(parents=True)
    scores = {"manifest_precision": 0.9, "manifest_recall": 0.5, "missed_silent": 2}
    (bench / "baseline.json").write_text(
        json.dumps({"apps": {"held": scores, "tuned": scores, "orphan": scores}, "pooled": scores, "pooled_held_out": scores}),
        encoding="utf-8",
    )
    (bench / "apps" / "held.json").write_text(json.dumps({"split": "held_out"}), encoding="utf-8")
    (bench / "apps" / "tuned.json").write_text(json.dumps({"app": "tuned"}), encoding="utf-8")


class ValidationSourcesTests(unittest.TestCase):
    def test_reads_benchmark_with_splits_and_intended_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_sources(root)
            benchmark = validation_sources.read_benchmark(root)
            intended = validation_sources.read_intended_use(root)
        self.assertTrue(benchmark.available)
        self.assertEqual(len(benchmark.sha256), 64)
        self.assertEqual({app.name: app.split for app in benchmark.apps}, {"held": "held_out", "orphan": "unknown", "tuned": "tuned"})
        self.assertEqual(benchmark.pooled_held_out and benchmark.pooled_held_out.missed_silent, 2)
        self.assertTrue(intended.available)
        self.assertIn("Advisory", intended.text)

    def test_missing_sources_are_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            benchmark = validation_sources.read_benchmark(Path(directory))
            intended = validation_sources.read_intended_use(Path(directory))
        self.assertFalse(benchmark.available)
        self.assertTrue(benchmark.problem)
        self.assertFalse(intended.available)

    def test_tool_version_prefers_environment(self) -> None:
        with patch.object(validation_sources.config, "TOOL_VERSION", "abc123"):
            self.assertEqual(validation_sources.read_tool_version(), ToolVersion(version="abc123", source="environment"))
        with patch.object(validation_sources.config, "TOOL_VERSION", ""), tempfile.TemporaryDirectory() as directory:
            # Not a git checkout: unknown, never an error.
            with patch.object(validation_sources.subprocess, "run", side_effect=OSError("no git")):
                self.assertEqual(validation_sources.read_tool_version(Path(directory)).source, "unknown")

    def test_repository_files_are_readable(self) -> None:
        # The bundle quotes these from the repository (and the showcase image).
        self.assertTrue(validation_sources.read_benchmark().available)
        self.assertTrue(validation_sources.read_intended_use().available)


class ValidationBundleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        _write_sources(self.root)
        review_store = ReviewStore(self.root / "r.sqlite3")
        review_store.append(SCOPE, _answer(reason="Seen in <code>"))
        review_store.append(SCOPE, _answer(answer_id="a2"))
        jobs = {
            "job-1": PrAssessmentJob(
                job_id="job-1", pr_id="PR-7", status="completed", tenant="acme", project_id="shop",
                assessment=_reviewed_assessment(),
            ),
        }
        baselines = MagicMock()
        baselines.read.side_effect = FileNotFoundError("gone")
        root = self.root
        for patcher in (
            patch.object(decision_api, "DECISION_STORE", DecisionStore(self.root / "d.sqlite3", vfs="")),
            patch.dict(webhooks._assessment_jobs, jobs, clear=True),
            patch.object(webhooks, "ASSESSMENT_ARCHIVE", None),
            patch.object(coordinator_nodes, "BASELINE_STORE", baselines),
            patch.object(coordinator_nodes, "REVIEW_STORE", review_store),
            patch.object(decision_api, "read_benchmark", lambda: validation_sources.read_benchmark(root)),
            patch.object(decision_api, "read_intended_use", lambda: validation_sources.read_intended_use(root)),
            patch.object(decision_api, "read_tool_version", lambda: ToolVersion(version="deadbeef", source="environment")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app)
        self.client.post(
            "/api/v1/webhooks/pr-assessment/job-1/decisions",
            json={"verdict": "approve", "reason": "ok", "signer_name": "Sam"},
        )

    def test_json_bundle_carries_every_part_and_is_sealed(self) -> None:
        response = self.client.get(BUNDLE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('filename="validation-package-PR-7-job-1.json"', response.headers["content-disposition"])
        bundle = ValidationBundle.model_validate_json(response.text)
        self.assertEqual(bundle.tool.version, "deadbeef")
        self.assertEqual(len(bundle.record.decisions), 1)
        self.assertTrue(bundle.record.chain.intact)
        # Only the answers the assessment names, in order; a vanished one is listed.
        self.assertEqual([a.answer_id for a in bundle.review.answers], ["a1"])
        self.assertEqual(bundle.review.missing, ["a-gone"])
        self.assertEqual(bundle.review.project_answers, 2)
        self.assertTrue(bundle.review.chain and bundle.review.chain.intact)
        self.assertTrue(bundle.benchmark.available)
        self.assertIn("Advisory", bundle.intended_use.text)
        self.assertEqual(bundle.bundle_sha256, bundle_digest(bundle))
        changed = json.loads(response.text)
        changed["intended_use"]["text"] = "edited"
        self.assertNotEqual(bundle_digest(ValidationBundle.model_validate(changed)), bundle.bundle_sha256)

    def test_print_view_shows_parts_escaped(self) -> None:
        response = self.client.get(BUNDLE, params={"format": "html"})
        self.assertEqual(response.status_code, 200)
        page = response.text
        self.assertIn("Validation package: PR-7", page)
        self.assertIn("deadbeef", page)
        self.assertIn("Approved for release: Sam", page)
        self.assertIn("Held-out apps (held)", page)
        self.assertIn("Seen in &lt;code&gt;", page)
        self.assertIn("Advisory &lt;only&gt;.", page)
        self.assertIn("a-gone", page)
        self.assertNotIn("<script", page)

    def test_unreadable_review_store_is_reported_in_the_bundle(self) -> None:
        broken = MagicMock()
        broken.answers.side_effect = OSError("share offline")
        with patch.object(coordinator_nodes, "REVIEW_STORE", broken):
            response = self.client.get(BUNDLE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ValidationBundle.model_validate_json(response.text).review.problem, "share offline")

    def test_unknown_or_unfinished_assessment(self) -> None:
        self.assertEqual(self.client.get("/api/v1/webhooks/pr-assessment/nope/validation-bundle").status_code, 404)


if __name__ == "__main__":
    unittest.main()

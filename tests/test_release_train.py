"""Release trains: several PR assessments combined into one release."""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from src import release_train_api, webhooks
from src.coordinator.sequencing import DependencyPair, ReleaseOrderFacts, sequence_deployment
from src.core.models import (
    AssessmentEvidence,
    ContractChange,
    ReasonCode,
    ReleaseAssessment,
    RiskAssessment,
)
from src.orchestration.release_train import (
    TrainIssueKind,
    TrainMemberInput,
    build_release_train,
)

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def _assessment(
    pr_id: str,
    targets: list[str],
    *,
    codes: list[ReasonCode] | None = None,
    snapshot: str = "snap-1",
    risk: float | None = 0.2,
    stale: list[str] | None = None,
) -> ReleaseAssessment:
    codes = codes or []
    approved = not codes
    return ReleaseAssessment(
        approved=approved,
        actionable=not any(code.limits_actionability for code in codes),
        reason_codes=codes,
        risk_score=risk,
        safe_order_established=approved,
        deployment_sequence=list(targets),
        services_to_deploy=list(targets),
        stale_consumers=stale or [],
        justification="why",
        advisory_assessment=RiskAssessment(risk_score=risk or 0.0, approved=True, justification="ok")
        if approved
        else None,
        evidence=AssessmentEvidence(pr_id=pr_id, repository=f"repo-{pr_id}", baseline_snapshot_id=snapshot),
    )


def _order(
    targets: list[str],
    edges: list[tuple[str, str]],
    change: ContractChange = ContractChange.ADDITIVE,
    upstream: list[str] | None = None,
    downstream: list[str] | None = None,
) -> ReleaseOrderFacts:
    return ReleaseOrderFacts(
        change_type=change,
        targets=targets,
        upstream=upstream or [],
        downstream=downstream or [],
        edges=[DependencyPair(source_service_id=a, target_service_id=b) for a, b in edges],
    )


def _member(
    job: str, assessment: ReleaseAssessment, order: ReleaseOrderFacts | None
) -> TrainMemberInput:
    return TrainMemberInput(
        job_id=job, pr_id=assessment.evidence.pr_id, assessment=assessment, order=order
    )


def _train(*members: TrainMemberInput):  # type: ignore[no-untyped-def]
    return build_release_train("t-1", "", "default", "shop", NOW, list(members))


class BuildReleaseTrainTest(unittest.TestCase):
    def test_combined_order_puts_providers_first_across_prs(self) -> None:
        # PR-1 changes web (calls api); PR-2 changes api (calls db).
        first = _member(
            "j1",
            _assessment("PR-1", ["web"]),
            _order(["web"], [("web", "api")], downstream=["api", "db"]),
        )
        second = _member(
            "j2",
            _assessment("PR-2", ["api"]),
            _order(["api"], [("web", "api"), ("api", "db")], upstream=["web"], downstream=["db"]),
        )
        train = _train(first, second)
        self.assertEqual(train.deployment_sequence, ["db", "api", "web"])
        self.assertEqual(train.services_to_deploy, ["api", "web"])
        self.assertEqual(train.downstream_services, ["db"])
        self.assertEqual(train.upstream_services, [])
        self.assertEqual(train.status, "approved")
        self.assertTrue(train.safe_order_established)
        self.assertEqual(train.issues, [])
        self.assertEqual([m.pr_id for m in train.members], ["PR-1", "PR-2"])

    def test_breaking_member_deploys_its_consumers_first(self) -> None:
        breaking = _member(
            "j1",
            _assessment("PR-1", ["api"]),
            _order(["api"], [("web", "api")], ContractChange.BREAKING, upstream=["web"]),
        )
        other = _member("j2", _assessment("PR-2", ["db"]), _order(["db"], []))
        train = _train(breaking, other)
        self.assertLess(train.deployment_sequence.index("web"), train.deployment_sequence.index("api"))
        self.assertEqual(train.members[0].change_type, ContractChange.BREAKING)

    def test_matches_single_pr_order_for_one_change_type(self) -> None:
        edges = [("web", "api"), ("api", "db"), ("web", "db")]
        facts = _order(["api"], edges, upstream=["web"], downstream=["db"])
        single = sequence_deployment(
            ["api"], ["web"], ["db"], facts.edges, ContractChange.ADDITIVE
        )
        train = _train(
            _member("j1", _assessment("PR-1", ["api"]), facts),
            _member("j2", _assessment("PR-2", ["db"]), _order(["db"], [("api", "db")])),
        )
        self.assertEqual(train.deployment_sequence, single.sequence)

    def test_worst_member_wins(self) -> None:
        go = _member("j1", _assessment("PR-1", ["a"]), _order(["a"], []))
        decide = _member(
            "j2", _assessment("PR-2", ["b"], codes=[ReasonCode.MODEL_REJECTED], risk=0.7), _order(["b"], [])
        )
        blocked = _member(
            "j3",
            _assessment("PR-3", ["c"], codes=[ReasonCode.DEPENDENCY_CYCLE], risk=None),
            _order(["c"], []),
        )
        self.assertEqual(_train(go, decide).status, "decision_needed")
        self.assertEqual(_train(go, decide).risk_score, 0.7)
        train = _train(go, decide, blocked)
        self.assertEqual(train.status, "blocked")
        self.assertFalse(train.actionable)
        self.assertIsNone(train.risk_score)
        self.assertEqual(train.reason_codes, [ReasonCode.DEPENDENCY_CYCLE, ReasonCode.MODEL_REJECTED])
        self.assertTrue(train.headline.startswith("No-go"))

    def test_train_issues_block_even_when_members_are_approved(self) -> None:
        mismatch = _train(
            _member("j1", _assessment("PR-1", ["a"]), _order(["a"], [])),
            _member("j2", _assessment("PR-2", ["b"], snapshot="snap-2"), None),
        )
        self.assertEqual(mismatch.status, "blocked")
        self.assertEqual(
            [i.kind for i in mismatch.issues],
            [TrainIssueKind.BASELINE_MISMATCH, TrainIssueKind.ORDER_FACTS_MISSING],
        )
        self.assertIn("b", mismatch.services_to_deploy)  # still ordered from the assessment
        self.assertFalse(mismatch.safe_order_established)

    def test_conflicting_orders_are_a_train_cycle(self) -> None:
        # Each PR's own radius is acyclic; together web and api call each other.
        train = _train(
            _member("j1", _assessment("PR-1", ["api"]), _order(["api"], [("web", "api")])),
            _member("j2", _assessment("PR-2", ["web"]), _order(["web"], [("api", "web")])),
        )
        self.assertEqual([i.kind for i in train.issues], [TrainIssueKind.TRAIN_CYCLE])
        self.assertEqual(train.cyclic_services, ["api", "web"])
        self.assertEqual(train.status, "blocked")

    def test_stale_consumer_changed_by_another_member(self) -> None:
        train = _train(
            _member("j1", _assessment("PR-1", ["api"], stale=["web", "mobile"]), _order(["api"], [])),
            _member("j2", _assessment("PR-2", ["web"]), _order(["web"], [])),
        )
        self.assertEqual(train.stale_consumers, ["mobile"])
        self.assertEqual(train.consumers_changed_in_train, ["web"])

    def test_needs_two_distinct_members(self) -> None:
        one = _member("j1", _assessment("PR-1", ["a"]), None)
        with self.assertRaises(ValueError):
            _train(one)
        with self.assertRaises(ValueError):
            _train(one, one)


class ReleaseTrainApiTest(unittest.TestCase):
    def setUp(self) -> None:
        for target in (webhooks._assessment_jobs, release_train_api._trains):
            patcher = patch.dict(target, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        archive = patch.object(webhooks, "ASSESSMENT_ARCHIVE", None)
        archive.start()
        self.addCleanup(archive.stop)

    def _job(self, job_id: str, pr_id: str, targets: list[str], **fields: object) -> None:
        values: dict[str, object] = {
            "job_id": job_id,
            "pr_id": pr_id,
            "status": "completed",
            "project_id": "shop",
            "assessment": _assessment(pr_id, targets),
            "order": _order(targets, []),
        }
        values.update(fields)
        webhooks._assessment_jobs[job_id] = webhooks.PrAssessmentJob.model_validate(values)

    def test_create_get_and_list(self) -> None:
        self._job("j1", "PR-1", ["a"])
        self._job("j2", "PR-2", ["b"])
        created = release_train_api.create_release_train(
            release_train_api.ReleaseTrainRequest(job_ids=["j1", "j2"], title="Sprint 12")
        )
        self.assertEqual((created.title, created.project_id, created.status), ("Sprint 12", "shop", "approved"))
        again = release_train_api.get_release_train(created.train_id)
        self.assertEqual(again, created)
        self.assertEqual([t.train_id for t in release_train_api.list_release_trains()], [created.train_id])

    def test_rejects_unknown_unfinished_and_mixed_projects(self) -> None:
        self._job("j1", "PR-1", ["a"])
        self._job("j2", "PR-2", ["b"], status="running", assessment=None, order=None)
        self._job("j3", "PR-3", ["c"], project_id="other")
        for job_ids, code in ((["j1", "nope"], 404), (["j1", "j2"], 409), (["j1", "j3"], 422)):
            with self.assertRaises(HTTPException) as raised:
                release_train_api.create_release_train(
                    release_train_api.ReleaseTrainRequest(job_ids=job_ids)
                )
            self.assertEqual(raised.exception.status_code, code)
        with self.assertRaises(HTTPException) as missing:
            release_train_api.get_release_train("nope")
        self.assertEqual(missing.exception.status_code, 404)
        with self.assertRaises(ValidationError):
            release_train_api.ReleaseTrainRequest(job_ids=["j1", "j1"])

    def test_completed_job_records_order_facts(self) -> None:
        assessment = _assessment("PR-9", ["api"])
        plan = sequence_deployment(["api"], ["web"], [], [], ContractChange.BREAKING)
        coordinator = MagicMock()
        coordinator.stream.return_value = iter(
            [
                {"blast": {"upstream_services": ["web"], "downstream_services": [], "matched_edges": []}},
                {"sequence_deployment_node": {"deployment_plan": plan}},
                {"compile_evidence_node": {"release_assessment": assessment}},
            ]
        )
        with patch.object(webhooks, "coordinator_app", coordinator):
            asyncio.run(webhooks.run_assessment_job("j9", "PR-9", {}))
        order = webhooks.get_pr_assessment("j9").order
        assert order is not None
        self.assertEqual((order.change_type, order.targets, order.upstream), (ContractChange.BREAKING, ["api"], ["web"]))


if __name__ == "__main__":
    unittest.main()

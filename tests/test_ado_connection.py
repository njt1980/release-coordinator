"""ADO connection: pull request polling (read-only) and the opt-in advisory
status and briefing comment written back to the pull request."""

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

from src import pr_watch_runner, webhooks
from src.core.models import ContractChange, DiscoveryResult
from src.orchestration.pr_watch import (
    IntakeAttempt,
    change_type_for,
    due_pull_requests,
    poll_project,
)
from src.tools.baseline_store import BaselineScope
from src.tools.devops_pr_feedback import (
    MAX_COMMENT_CHARS,
    STATUS_GENRE,
    AssessmentOutcome,
    PullRequestTarget,
    comment_text,
    post_feedback,
    status_state,
)
from src.tools.devops_pull_requests import (
    ActivePullRequest,
    active_pull_requests,
    list_active_pull_requests,
)
from src.tools.policy_store import (
    AdoConnection,
    PolicyInvalidError,
    PolicyStore,
    parse_policy,
)
from src.tools.pr_watch_store import PrWatchStore, SeenPullRequest
from tests import test_pr_webhook as webhook_tests
from tests.test_pr_intake import DETAILS
from tests.test_pr_webhook import POLICY_YAML

SCOPE = BaselineScope(tenant="acme", project_id="shop")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TARGET = PullRequestTarget(
    organization_url="https://dev.azure.com/acme", project="Shop", repository_id="repo-id", pr_id=17, iteration_id=3
)


def _active(pr_id: int = 17, commit: str = "c1", branch: str = "main", labels: list[str] | None = None) -> ActivePullRequest:
    return ActivePullRequest(
        pr_id=pr_id,
        repository="Shop",
        repository_id="repo-id",
        source_commit=commit,
        target_branch=branch,
        labels=labels or [],
    )


def _seen(pr_id: int = 17, commit: str = "c1", branch: str = "main", outcome: str = "scheduled") -> SeenPullRequest:
    return SeenPullRequest(
        repository_id="repo-id",
        pr_id=pr_id,
        source_commit=commit,
        target_branch=branch,
        handled_at=datetime(2026, 11, 14, tzinfo=UTC),
        outcome=outcome,  # type: ignore[arg-type]
    )


class PolicyTests(unittest.TestCase):
    def test_everything_is_off_by_default(self) -> None:
        connection = parse_policy(POLICY_YAML, SCOPE).ado_connection
        self.assertEqual((connection.poll, connection.comment, connection.status), (False, False, False))
        self.assertFalse(connection.writes)

    def test_polling_needs_repositories_and_source_control(self) -> None:
        with self.assertRaises(ValueError):
            AdoConnection(poll=True)
        without_source = POLICY_YAML.split("source_control:")[0] + "ado_connection: {status: true}\n"
        with self.assertRaises(PolicyInvalidError):
            parse_policy(without_source, SCOPE)
        enabled = parse_policy(POLICY_YAML + "ado_connection:\n  poll: true\n  repositories: [Shop]\n", SCOPE)
        self.assertTrue(enabled.ado_connection.poll)

    def test_no_checked_in_policy_polls_or_writes(self) -> None:
        # The showcase and the lab ship these policies: nothing may poll or
        # write to Azure DevOps unless someone adds a policy entry.
        store = PolicyStore(REPOSITORY_ROOT / "policies")
        scopes = store.list_scopes()
        self.assertTrue(scopes)
        self.assertEqual(pr_watch_runner.polling_policies(store), [])
        for scope in scopes:
            self.assertFalse(store.load(scope).policy.ado_connection.writes, scope)


class ListingTests(unittest.TestCase):
    def test_active_entries_with_commit_and_labels(self) -> None:
        entries = [
            SimpleNamespace(
                pull_request_id=17,
                status="active",
                last_merge_source_commit=SimpleNamespace(commit_id="ABC"),
                target_ref_name="refs/heads/main",
                title="t",
                labels=[SimpleNamespace(name="rc:breaking")],
            ),
            SimpleNamespace(pull_request_id=18, status="active", last_merge_source_commit=None),
            SimpleNamespace(
                pull_request_id=19, status="completed", last_merge_source_commit=SimpleNamespace(commit_id="d")
            ),
        ]
        found = active_pull_requests(entries, "Shop", "repo-id")
        self.assertEqual([(p.pr_id, p.source_commit, p.target_branch, p.labels) for p in found], [(17, "abc", "main", ["rc:breaking"])])

    def test_pages_until_a_short_page(self) -> None:
        git = MagicMock()
        page = [
            SimpleNamespace(pull_request_id=i, status="active", last_merge_source_commit=SimpleNamespace(commit_id="c"))
            for i in range(1, 3)
        ]
        git.get_pull_requests.side_effect = [page, page[:1]]
        found = list_active_pull_requests(git, "Shop", SimpleNamespace(id="repo-id", name="Shop"), "criteria", page_size=2)
        self.assertEqual(len(found), 3)
        self.assertEqual(git.get_pull_requests.call_args_list[1].kwargs["skip"], 2)


class DueTests(unittest.TestCase):
    def test_new_pushed_retargeted_and_failed_iterations_are_due(self) -> None:
        seen = {
            s.key: s
            for s in [
                _seen(1, "old"),
                _seen(2, "same"),
                _seen(3, "same", branch="release"),
                _seen(4, "same", outcome="failed"),
                _seen(5, "same", outcome="skipped"),
            ]
        }
        active = [_active(1, "new"), _active(2, "same"), _active(3, "same"), _active(4, "same"), _active(5, "same"), _active(6)]
        self.assertEqual([p.pr_id for p in due_pull_requests(active, seen, [])], [1, 3, 4, 6])

    def test_target_branch_filter(self) -> None:
        active = [_active(1, branch="main"), _active(2, branch="feature/x")]
        self.assertEqual([p.pr_id for p in due_pull_requests(active, {}, ["Main"])], [1])

    def test_labels_override_the_policy_change_type(self) -> None:
        self.assertIs(change_type_for(_active(labels=["RC:Breaking"]), ContractChange.ADDITIVE), ContractChange.BREAKING)
        self.assertIs(change_type_for(_active(labels=["rc:additive"]), ContractChange.BREAKING), ContractChange.ADDITIVE)
        self.assertIs(change_type_for(_active(), ContractChange.BREAKING), ContractChange.BREAKING)


class PollProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = PrWatchStore(Path(tmp.name))
        self.connection = AdoConnection(poll=True, repositories=["Shop"], change_type=ContractChange.BREAKING)

    def test_each_iteration_is_assessed_once(self) -> None:
        calls: list[tuple[int, ContractChange]] = []

        def intake(pr: ActivePullRequest, change: ContractChange) -> IntakeAttempt:
            calls.append((pr.pr_id, change))
            return IntakeAttempt(outcome="scheduled", job_id=f"job-{pr.pr_id}")

        listing = DiscoveryResult[ActivePullRequest].success([_active(17), _active(18, labels=["rc:additive"])])
        report = poll_project(SCOPE, self.connection, lambda repos: listing, intake, self.store)
        self.assertEqual(calls, [(17, ContractChange.BREAKING), (18, ContractChange.ADDITIVE)])
        self.assertEqual(report.listed, 2)
        poll_project(SCOPE, self.connection, lambda repos: listing, intake, self.store)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.load(SCOPE).entries["repo-id/17"].job_id, "job-17")

    def test_closed_pull_requests_are_forgotten_and_failed_listing_keeps_state(self) -> None:
        intake = lambda pr, change: IntakeAttempt(outcome="skipped", detail="no service")  # noqa: E731
        poll_project(
            SCOPE, self.connection, lambda r: DiscoveryResult[ActivePullRequest].success([_active(17)]), intake, self.store
        )
        poll_project(SCOPE, self.connection, lambda r: DiscoveryResult[ActivePullRequest].failed("TF400813"), intake, self.store)
        self.assertIn("repo-id/17", self.store.load(SCOPE).entries)
        poll_project(SCOPE, self.connection, lambda r: DiscoveryResult[ActivePullRequest].success([]), intake, self.store)
        self.assertEqual(self.store.load(SCOPE).entries, {})


class FeedbackTests(unittest.TestCase):
    def test_status_follows_the_verdict(self) -> None:
        self.assertEqual(status_state(AssessmentOutcome(job_id="j", approved=True, actionable=True)), "succeeded")
        self.assertEqual(status_state(AssessmentOutcome(job_id="j", actionable=True)), "pending")
        self.assertEqual(status_state(AssessmentOutcome(job_id="j")), "failed")
        self.assertEqual(status_state(AssessmentOutcome(job_id="j", failed=True)), "error")

    def test_iteration_status_and_closed_thread(self) -> None:
        git = MagicMock()
        outcome = AssessmentOutcome(job_id="j1", actionable=True, headline="Your call", briefing_markdown="## Briefing")
        result = post_feedback(git, TARGET, outcome, status=True, comment=True, link_base_url="https://rc.example")
        self.assertTrue(result.status_posted and result.comment_posted)
        status_payload, repository_id, pr_id, iteration_id = git.create_pull_request_iteration_status.call_args.args
        self.assertEqual((repository_id, pr_id, iteration_id), ("repo-id", 17, 3))
        self.assertEqual((status_payload.context.genre, status_payload.state), (STATUS_GENRE, "pending"))
        self.assertIn("advisory", status_payload.description)
        self.assertEqual(status_payload.target_url, "https://rc.example/api/v1/webhooks/pr-assessment/j1/record?format=html")
        thread = git.create_thread.call_args.args[0]
        self.assertEqual(thread.status, "closed")
        self.assertIn("## Briefing", thread.comments[0].content)

    def test_write_failures_are_returned_not_raised(self) -> None:
        git = MagicMock()
        git.create_pull_request_iteration_status.side_effect = RuntimeError("TF401027: needs PullRequestContribute")
        result = post_feedback(git, TARGET, AssessmentOutcome(job_id="j"), status=True, comment=False)
        self.assertFalse(result.status_posted)
        self.assertIn("TF401027", result.errors[0])
        git.create_thread.assert_not_called()

    def test_long_briefings_are_truncated(self) -> None:
        text = comment_text(AssessmentOutcome(job_id="j", briefing_markdown="x" * (MAX_COMMENT_CHARS * 2)), 1)
        self.assertLessEqual(len(text), MAX_COMMENT_CHARS)
        self.assertTrue(text.endswith("_(briefing truncated)_"))


class IntakeFeedbackTests(webhook_tests.BaselineFixture):
    def _intake(self) -> str:
        details = DETAILS.model_copy(update={"repository_id": "repo-id", "iteration_id": 3})
        extractor = MagicMock()
        extractor.read_pull_request.return_value = DiscoveryResult.success([details])
        with patch.object(webhooks, "DevOpsExtractor", return_value=extractor):
            request = webhooks.PrIntakeRequest(pr_id=17, change_type=ContractChange.ADDITIVE, tenant="acme", project_id="shop")
            accepted = webhooks.intake_pull_request(request, BackgroundTasks())
        return accepted.job_id

    def _complete(self, job_id: str) -> None:
        record = webhooks._assessment_jobs[job_id]
        webhooks._assessment_jobs[job_id] = record.model_copy(update={"status": "completed"})

    def test_intake_records_the_iteration_it_read(self) -> None:
        self._write_baseline()
        job_id = self._intake()
        self.assertEqual(webhooks._assessment_jobs[job_id].source, TARGET)

    def test_nothing_is_written_by_default(self) -> None:
        self._write_baseline()
        job_id = self._intake()
        self._complete(job_id)
        factory = MagicMock()
        with patch.object(webhooks, "FEEDBACK_CLIENT", factory):
            self.assertIsNone(webhooks.publish_feedback(job_id))
        factory.assert_not_called()

    def test_opted_in_feedback_is_posted_and_recorded(self) -> None:
        self._write_policy(POLICY_YAML + "ado_connection: {status: true, comment: true}\n")
        self._write_baseline()
        job_id = self._intake()
        self._complete(job_id)
        git = MagicMock()
        with patch.object(webhooks, "FEEDBACK_CLIENT", return_value=git) as factory:
            result = webhooks.publish_feedback(job_id)
        factory.assert_called_once_with("https://dev.azure.com/acme")
        assert result is not None
        self.assertEqual((result.status_state, result.status_posted, result.comment_posted), ("failed", True, True))
        self.assertEqual(webhooks._assessment_jobs[job_id].ado_feedback, result)

    def test_never_writes_outside_the_policy_project(self) -> None:
        self._write_policy(POLICY_YAML + "ado_connection: {status: true}\n")
        self._write_baseline()
        job_id = self._intake()
        self._complete(job_id)
        record = webhooks._assessment_jobs[job_id]
        assert record.source is not None
        moved = record.source.model_copy(update={"project": "Other"})
        webhooks._assessment_jobs[job_id] = record.model_copy(update={"source": moved})
        factory = MagicMock()
        with patch.object(webhooks, "FEEDBACK_CLIENT", factory):
            result = webhooks.publish_feedback(job_id)
        factory.assert_not_called()
        assert result is not None
        self.assertFalse(result.status_posted)


class RunnerTests(webhook_tests.BaselineFixture):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("POLICY_STORE", self.policy_store), ("WATCH_STORE", PrWatchStore(Path(tmp.name)))):
            patcher = patch.object(pr_watch_runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_policies_that_turn_polling_on_are_polled(self) -> None:
        self.assertEqual(pr_watch_runner.polling_policies(self.policy_store), [])
        self._write_policy(POLICY_YAML + "ado_connection:\n  poll: true\n  repositories: [Shop]\n")
        self.assertEqual([scope for scope, _ in pr_watch_runner.polling_policies(self.policy_store)], [self.scope])

    def test_a_pass_schedules_intake_and_classifies_refusals(self) -> None:
        policy = self._write_policy(POLICY_YAML + "ado_connection:\n  poll: true\n  repositories: [Shop]\n").policy
        extractor = MagicMock()
        extractor.list_active_pull_requests.return_value = DiscoveryResult.success([_active(17), _active(18)])
        refusals = {18: HTTPException(status_code=422, detail="no service")}

        def intake(request: Any, tasks: BackgroundTasks) -> Any:
            if request.pr_id in refusals:
                raise refusals[request.pr_id]
            return SimpleNamespace(job_id=f"job-{request.pr_id}")

        with patch.object(pr_watch_runner, "DevOpsExtractor", return_value=extractor), patch.object(
            pr_watch_runner, "intake_pull_request", side_effect=intake
        ):
            report = asyncio.run(pr_watch_runner.poll_once(self.scope, policy))
        outcomes = {item.pr_id: item.attempt.outcome for item in report.handled}
        self.assertEqual(outcomes, {17: "scheduled", 18: "skipped"})
        extractor.list_active_pull_requests.assert_called_once_with(["Shop"])


if __name__ == "__main__":
    unittest.main()

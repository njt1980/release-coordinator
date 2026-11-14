"""Assess a pull request by its Azure DevOps ID (step 2b+ PR intake).

The pull request is read (read-only) at its latest iteration; its diff is
built from each changed file's text at the merge base and the head.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

from src import webhooks
from src.core.models import ContractChange, DiscoveryResult
from src.tools.baseline_store import BaselineScope, BaselineStore
from src.tools.devops_pull_requests import (
    MAX_FILE_CHARS,
    FileChange,
    PullRequestDetails,
    PullRequestError,
    PullRequestFile,
    build_diff,
    changed_files,
    read_pull_request,
)
from src.tools.policy_store import PolicyStore
from tests import test_pr_webhook as webhook_tests
from tests.test_pr_webhook import BASE, HEAD, POLICY_YAML


def _entry(path: str, change: str, original: str | None = None, folder: bool = False) -> Any:
    return SimpleNamespace(
        item={"path": path, "isFolder": folder}, change_type=change, original_path=original
    )


class ChangedFilesTests(unittest.TestCase):
    def test_change_flags_renames_and_folders(self) -> None:
        files = changed_files(
            [
                _entry("/src/api/app.js", "edit"),
                _entry("/src/api/new.js", "add"),
                _entry("/src/api/old.js", "delete"),
                _entry("/src/api/b.js", "edit, rename", original="/src/api/a.js"),
                _entry("/src/api", "edit", folder=True),
            ]
        )
        self.assertEqual(
            files,
            [
                PullRequestFile(path="src/api/app.js", change=FileChange.EDIT),
                PullRequestFile(path="src/api/new.js", change=FileChange.ADD),
                PullRequestFile(path="src/api/old.js", change=FileChange.DELETE),
                PullRequestFile(path="src/api/b.js", change=FileChange.RENAME, original_path="src/api/a.js"),
            ],
        )


class BuildDiffTests(unittest.TestCase):
    TEXT = {
        ("a.txt", "base"): "one\ntwo\n",
        ("a.txt", "head"): "one\nthree\n",
        ("new.txt", "head"): "fresh\n",
        ("gone.txt", "base"): "bye\n",
        ("img.png", "base"): "\x89PNG\x00",
        ("img.png", "head"): "\x89PNG\x00\x01",
        ("big.txt", "base"): "x" * (MAX_FILE_CHARS + 1),
        ("big.txt", "head"): "y",
    }

    def read(self, path: str, commit: str) -> str | None:
        return self.TEXT.get((path, commit))

    def test_unified_diff_of_text_files_only(self) -> None:
        files = [
            PullRequestFile(path="a.txt", change=FileChange.EDIT),
            PullRequestFile(path="new.txt", change=FileChange.ADD),
            PullRequestFile(path="gone.txt", change=FileChange.DELETE),
            PullRequestFile(path="img.png", change=FileChange.EDIT),
            PullRequestFile(path="big.txt", change=FileChange.EDIT),
        ]
        diff, truncated, skipped = build_diff(files, "base", "head", self.read)
        self.assertIn("--- a/a.txt\n+++ b/a.txt\n", diff)
        self.assertIn("-two\n+three\n", diff)
        self.assertIn("--- /dev/null\n+++ b/new.txt\n", diff)
        self.assertIn("--- a/gone.txt\n+++ /dev/null\n", diff)
        self.assertFalse(truncated)
        self.assertEqual(skipped, ["img.png", "big.txt"])

    def test_files_past_the_limit_are_skipped(self) -> None:
        files = [PullRequestFile(path="a.txt", change=FileChange.EDIT)] * 3
        with patch("src.tools.devops_pull_requests.MAX_DIFF_FILES", 2):
            _, truncated, skipped = build_diff(files, "base", "head", self.read)
        self.assertTrue(truncated)
        self.assertEqual(skipped, ["a.txt"])


def _git(status: str = "active", iterations: int = 2) -> MagicMock:
    git = MagicMock()
    git.get_pull_request_by_id.return_value = SimpleNamespace(
        status=status,
        title="Add discount",
        description="desc",
        source_ref_name="refs/heads/feature/discount",
        target_ref_name="refs/heads/main",
        repository=SimpleNamespace(id="repo-id", name="Shop"),
        last_merge_source_commit=None,
        last_merge_target_commit=None,
    )
    git.get_pull_request_iterations.return_value = [
        SimpleNamespace(
            id=number,
            source_ref_commit={"commitId": HEAD.upper() if number == iterations else "f" * 40},
            common_ref_commit={"commitId": BASE},
        )
        for number in range(1, iterations + 1)
    ]
    git.get_pull_request_iteration_changes.return_value = SimpleNamespace(
        change_entries=[_entry("/src/order-service/routes/root.js", "edit")], next_skip=0
    )
    return git


class ReadPullRequestTests(unittest.TestCase):
    def test_latest_iteration_commits_branches_and_diff(self) -> None:
        git = _git()
        reads: list[tuple[str, str, str]] = []

        def reader_for(repository_id: str) -> Any:
            def read(path: str, commit: str) -> str:
                reads.append((repository_id, path, commit))
                return "old\n" if commit == BASE else "new\n"

            return read

        details = read_pull_request(git, "Shop", 17, reader_for)
        self.assertEqual((details.base_sha, details.head_sha), (BASE, HEAD))
        self.assertEqual((details.source_branch, details.target_branch), ("feature/discount", "main"))
        self.assertEqual(details.changed_paths, ["src/order-service/routes/root.js"])
        self.assertIn("-old\n+new\n", details.diff)
        git.get_pull_request_iteration_changes.assert_called_once_with(
            "repo-id", 17, 2, project="Shop", top=1000, skip=0
        )
        self.assertEqual({r[0] for r in reads}, {"repo-id"})

    def test_abandoned_or_empty_pull_requests_are_refused(self) -> None:
        with self.assertRaises(PullRequestError):
            read_pull_request(_git(status="abandoned"), "Shop", 17, lambda _: lambda *_: None)
        with self.assertRaises(PullRequestError):
            read_pull_request(_git(iterations=0), "Shop", 17, lambda _: lambda *_: None)


DETAILS = PullRequestDetails(
    pr_id=17,
    repository="Shop",
    title="Add discount",
    base_sha=BASE,
    head_sha=HEAD,
    source_branch="feature/discount",
    target_branch="main",
    files=[
        PullRequestFile(path="src/order-service/routes/root.js", change=FileChange.EDIT),
        PullRequestFile(path="docs/a.md", change=FileChange.RENAME, original_path="docs/old.md"),
    ],
    diff="--- a/x\n+++ b/x\n",
    skipped_files=["docs/a.md"],
)


class PrIntakeEndpointTests(webhook_tests.BaselineFixture):
    def _intake(self, result: DiscoveryResult[PullRequestDetails]) -> Any:
        extractor = MagicMock()
        extractor.read_pull_request.return_value = result
        with patch.object(webhooks, "DevOpsExtractor", return_value=extractor) as constructor:
            tasks = BackgroundTasks()
            request = webhooks.PrIntakeRequest(
                pr_id=17, change_type=ContractChange.ADDITIVE, tenant="acme", project_id="shop"
            )
            response = webhooks.pr_intake(request, tasks)
        constructor.assert_called_once_with(org_url="https://dev.azure.com/acme", project="Shop", repositories=[])
        return response, tasks

    def test_reads_the_pull_request_and_schedules_its_assessment(self) -> None:
        snapshot_id = self._write_baseline()
        response, tasks = self._intake(DiscoveryResult[PullRequestDetails].success([DETAILS]))
        self.assertEqual((response.pr_id, response.repository, response.baseline_snapshot_id), ("17", "Shop", snapshot_id))
        self.assertEqual(response.skipped_files, ["docs/a.md"])
        event = tasks.tasks[0].args[2]["trigger_event"]
        self.assertEqual((event["base_sha"], event["head_sha"], event["change_type"]), (BASE, HEAD, "additive"))
        self.assertEqual(event["changed_files"], ["src/order-service/routes/root.js", "docs/a.md", "docs/old.md"])
        self.assertEqual(event["source_branch"], "feature/discount")

    def test_an_unreadable_pull_request_is_a_bad_gateway(self) -> None:
        self._write_baseline()
        with self.assertRaises(HTTPException) as raised:
            self._intake(DiscoveryResult[PullRequestDetails].failed("pull request 17: TF401180 not found"))
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("TF401180", str(raised.exception.detail))

    def test_without_source_control_nothing_is_read(self) -> None:
        self._write_policy(POLICY_YAML.split("source_control:")[0] + "exemptions: {files: []}\n")
        with patch.object(webhooks, "DevOpsExtractor") as constructor, self.assertRaises(HTTPException) as raised:
            webhooks.pr_intake(
                webhooks.PrIntakeRequest(pr_id=17, change_type=ContractChange.ADDITIVE, tenant="acme", project_id="shop"),
                BackgroundTasks(),
            )
        self.assertEqual(raised.exception.status_code, 409)
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()

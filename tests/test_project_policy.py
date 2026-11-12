"""Tests for per-project release policies and the eligibility checks."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError

from src.coordinator import nodes as coordinator_nodes
from src.coordinator.project_policy import (
    baseline_freshness_issue,
    baseline_runtime_state,
    dependency_exemption,
    is_normalized_relative_path,
    partition_exempt_files,
    required_source_issues,
    runtime_evidence_issue,
)
from src.core.models import (
    ContractChange,
    DiscoveryMethod,
    DiscoveryResult,
    DiscoveryStageReport,
    DiscoveryStatus,
    Exemption,
    PullRequestPayload,
    ReasonCode,
    RuntimeEvidenceState,
)
from src.tools.baseline_store import BaselineScope, snapshot_created_at
from src.tools.policy_store import (
    DEFAULT_POLICIES_ROOT,
    LoadedPolicy,
    PolicyInvalidError,
    PolicyMissingError,
    PolicyStore,
    ProjectPolicy,
    RuntimeEvidenceRequirement,
    parse_policy,
)

SCOPE = BaselineScope(tenant="acme", project_id="shop")
POLICY_YAML = """\
policy_version: 2
tenant: acme
project_id: shop
max_baseline_age_hours: 12
required_sources: [STATIC_CODE, RUNTIME_TELEMETRY]
runtime_evidence: live
exemptions:
  files:
    - pattern: "docs/*"
      reason: "Documentation only"
    - pattern: "*.md"
      reason: "Markdown"
  dependencies:
    - entity_id: "unresolved:api.stripe.com"
      reason: "External payment API"
"""
POLICY = parse_policy(POLICY_YAML, SCOPE)
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
TARGET = "urn:release-coord:acme:unknown:unknown:shop:order-service"


def _reports(**statuses: DiscoveryStatus) -> list[DiscoveryStageReport]:
    return [
        DiscoveryStageReport(source=DiscoveryMethod[name], status=status)
        for name, status in statuses.items()
    ]


class PolicyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = PolicyStore(Path(tmp.name))

    def _write(self, text: str) -> None:
        path = self.store.policy_path(SCOPE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_loads_valid_policy_with_digest(self) -> None:
        self._write(POLICY_YAML)

        loaded = self.store.load(SCOPE)

        self.assertEqual(loaded.policy, POLICY)
        self.assertEqual(loaded.policy.policy_version, 2)
        self.assertEqual(loaded.policy.max_baseline_age_hours, 12)
        self.assertEqual(len(loaded.sha256), 64)
        self.assertEqual(self.store.policy_path(SCOPE).parts[-2:], ("acme", "shop.yaml"))

    def test_missing_policy_raises(self) -> None:
        with self.assertRaises(PolicyMissingError):
            self.store.load(SCOPE)

    def test_invalid_policies_raise(self) -> None:
        cases = {
            "malformed yaml": "policy_version: [1\n",
            "not a mapping": "- 1\n",
            "unknown key": POLICY_YAML + "approve_everything: true\n",
            "other tenant": POLICY_YAML.replace("tenant: acme", "tenant: evil"),
            "other project": POLICY_YAML.replace("project_id: shop", "project_id: x"),
            "age over cap": POLICY_YAML.replace(": 12", ": 48"),
            "zero age": POLICY_YAML.replace(": 12", ": 0"),
            "no sources": POLICY_YAML.replace(
                "[STATIC_CODE, RUNTIME_TELEMETRY]", "[]"
            ),
            "repeated source": POLICY_YAML.replace(
                "[STATIC_CODE, RUNTIME_TELEMETRY]", "[STATIC_CODE, STATIC_CODE]"
            ),
            "unknown source": POLICY_YAML.replace("STATIC_CODE", "GUESSWORK"),
            "unknown runtime": POLICY_YAML.replace(
                "runtime_evidence: live", "runtime_evidence: vibes"
            ),
            "missing version": POLICY_YAML.replace("policy_version: 2\n", ""),
            "blank reason": POLICY_YAML.replace(
                '"Documentation only"', '""'
            ),
            "entity without prefix": POLICY_YAML.replace(
                "unresolved:api.stripe.com", "api.stripe.com"
            ),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self._write(text)
                with self.assertRaises(PolicyInvalidError):
                    self.store.load(SCOPE)

    def test_non_utf8_policy_is_invalid(self) -> None:
        path = self.store.policy_path(SCOPE)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe")
        with self.assertRaises(PolicyInvalidError):
            self.store.load(SCOPE)

    def test_shipped_default_policy_is_strict(self) -> None:
        loaded = PolicyStore(DEFAULT_POLICIES_ROOT).load(BaselineScope())

        policy = loaded.policy
        self.assertEqual(policy.max_baseline_age_hours, 24)
        self.assertEqual(set(policy.required_sources), set(DiscoveryMethod))
        self.assertIs(policy.runtime_evidence, RuntimeEvidenceRequirement.LIVE)
        self.assertEqual(policy.exemptions.files, [])
        self.assertEqual(policy.exemptions.dependencies, [])


class FreshnessTests(unittest.TestCase):
    def test_snapshot_ids_encode_creation_time(self) -> None:
        self.assertEqual(
            snapshot_created_at("20261112T000000000000Z-abcd1234"),
            datetime(2026, 11, 12, tzinfo=timezone.utc),
        )
        self.assertIsNone(snapshot_created_at("snap-1"))

    def test_snapshot_at_the_limit_is_fresh(self) -> None:
        created = NOW - timedelta(hours=12)
        self.assertIsNone(baseline_freshness_issue(POLICY, "s", created, NOW))

    def test_snapshot_past_the_limit_is_stale(self) -> None:
        created = NOW - timedelta(hours=12, seconds=1)

        issue = baseline_freshness_issue(POLICY, "s", created, NOW)

        assert issue is not None
        self.assertIs(issue.code, ReasonCode.BASELINE_STALE)
        self.assertIn("12.0h old", issue.detail)
        self.assertIn("at most 12h", issue.detail)

    def test_snapshot_of_unknown_age_is_stale(self) -> None:
        issue = baseline_freshness_issue(POLICY, "snap-1", None, NOW)
        assert issue is not None
        self.assertIs(issue.code, ReasonCode.BASELINE_STALE)


class RequiredSourceTests(unittest.TestCase):
    def test_required_sources_must_succeed(self) -> None:
        reports = _reports(
            STATIC_CODE=DiscoveryStatus.SUCCESS,
            K8S_MANIFEST=DiscoveryStatus.SKIPPED,
            RUNTIME_TELEMETRY=DiscoveryStatus.SUCCESS,
        )
        # K8S_MANIFEST is skipped but not required.
        self.assertEqual(required_source_issues(POLICY, "s", reports), [])

    def test_skipped_or_missing_required_source_is_an_issue(self) -> None:
        reports = _reports(RUNTIME_TELEMETRY=DiscoveryStatus.SKIPPED)

        issues = required_source_issues(POLICY, "s", reports)

        self.assertEqual(
            [issue.code for issue in issues],
            [ReasonCode.REQUIRED_SOURCE_MISSING] * 2,
        )
        self.assertIn("STATIC_CODE evidence, but that stage is not reported", issues[0].detail)
        self.assertIn("RUNTIME_TELEMETRY evidence, but that stage is SKIPPED", issues[1].detail)

    def test_no_discovery_record_fails_every_source(self) -> None:
        self.assertEqual(len(required_source_issues(POLICY, "s", None)), 2)


class RuntimeEvidenceTests(unittest.TestCase):
    def test_baseline_runtime_state(self) -> None:
        self.assertIs(
            baseline_runtime_state(_reports(RUNTIME_TELEMETRY=DiscoveryStatus.SUCCESS)),
            RuntimeEvidenceState.BASELINE,
        )
        self.assertIs(
            baseline_runtime_state(_reports(RUNTIME_TELEMETRY=DiscoveryStatus.SKIPPED)),
            RuntimeEvidenceState.NONE,
        )
        self.assertIs(baseline_runtime_state(None), RuntimeEvidenceState.NONE)

    def test_requirement_matrix(self) -> None:
        baseline_policy = POLICY.model_copy(
            update={"runtime_evidence": RuntimeEvidenceRequirement.BASELINE}
        )
        accepted = [
            (POLICY, {RuntimeEvidenceState.LIVE}),
            (
                baseline_policy,
                {RuntimeEvidenceState.LIVE, RuntimeEvidenceState.BASELINE},
            ),
        ]
        for policy, states in accepted:
            for state in RuntimeEvidenceState:
                with self.subTest(policy=policy.runtime_evidence, state=state):
                    issue = runtime_evidence_issue(policy, state)
                    if state in states:
                        self.assertIsNone(issue)
                    else:
                        assert issue is not None
                        self.assertIs(issue.code, ReasonCode.REQUIRED_SOURCE_MISSING)


class ExemptionTests(unittest.TestCase):
    def test_matching_files_are_exempted_with_first_rule(self) -> None:
        remaining, exempted = partition_exempt_files(
            POLICY, ["docs/a/README.md", "src/app.py", "CHANGELOG.md"]
        )

        self.assertEqual(remaining, ["src/app.py"])
        self.assertEqual(
            exempted,
            [
                Exemption(
                    subject="docs/a/README.md", rule="docs/*", reason="Documentation only"
                ),
                Exemption(subject="CHANGELOG.md", rule="*.md", reason="Markdown"),
            ],
        )

    def test_unnormalized_paths_are_never_exempt(self) -> None:
        paths = [
            "docs/../src/app.py",
            "./docs/x",
            "/docs/x",
            "docs//x",
            "docs\\x.md",
            "",
        ]
        for path in paths:
            with self.subTest(path):
                self.assertFalse(is_normalized_relative_path(path))
        remaining, exempted = partition_exempt_files(POLICY, paths)
        self.assertEqual(remaining, paths)
        self.assertEqual(exempted, [])

    def test_no_policy_exempts_nothing(self) -> None:
        self.assertEqual(partition_exempt_files(None, ["docs/x"]), (["docs/x"], []))
        self.assertIsNone(dependency_exemption(None, "unresolved:api.stripe.com"))

    def test_dependency_exemption_matches_exact_entity(self) -> None:
        exemption = dependency_exemption(POLICY, "unresolved:api.stripe.com")
        assert exemption is not None
        self.assertEqual(exemption.reason, "External payment API")
        self.assertIsNone(dependency_exemption(POLICY, "unresolved:api.stripe.com.evil"))

    def test_unresolved_gaps_skip_exempt_dependencies(self) -> None:
        unresolved = [
            coordinator_nodes.UnresolvedReach(
                node="unresolved:api.stripe.com", detail="stripe"
            ),
            coordinator_nodes.UnresolvedReach(node="unresolved:10.0.0.1", detail="ip"),
        ]

        issues, exempted = coordinator_nodes.unresolved_gaps(POLICY, unresolved)

        self.assertEqual([issue.detail for issue in issues], ["ip"])
        self.assertEqual([e.subject for e in exempted], ["unresolved:api.stripe.com"])


class PolicyNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = PolicyStore(Path(tmp.name))
        patcher = patch.object(coordinator_nodes, "POLICY_STORE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state: Any = {"tenant": "acme", "project_id": "shop", "workspace_id": ""}

    def test_load_policy_node_records_policy(self) -> None:
        path = self.store.policy_path(SCOPE)
        path.parent.mkdir(parents=True)
        path.write_text(POLICY_YAML, encoding="utf-8")

        result = coordinator_nodes.load_policy_node(self.state)

        loaded = result["project_policy"]
        assert isinstance(loaded, LoadedPolicy)
        self.assertEqual(loaded.policy, POLICY)
        self.assertEqual(result["discovery_issues"], [])

    def test_load_policy_node_reports_missing_and_invalid(self) -> None:
        with self.assertLogs(coordinator_nodes.logger, level="WARNING"):
            missing = coordinator_nodes.load_policy_node(self.state)
        path = self.store.policy_path(SCOPE)
        path.parent.mkdir(parents=True)
        path.write_text("tenant: [", encoding="utf-8")
        with self.assertLogs(coordinator_nodes.logger, level="WARNING"):
            invalid = coordinator_nodes.load_policy_node(self.state)

        for result, code in [
            (missing, ReasonCode.POLICY_MISSING),
            (invalid, ReasonCode.POLICY_INVALID),
        ]:
            self.assertIsNone(result["project_policy"])
            issues = result["discovery_issues"]
            assert isinstance(issues, list)
            self.assertEqual([issue.code for issue in issues], [code])

    def test_trigger_node_moves_exempt_files_out_of_unmapped(self) -> None:
        pull_request = PullRequestPayload(
            pr_id="PR-1",
            title="t",
            target_service=TARGET,
            change_type=ContractChange.ADDITIVE,
            diff="d",
        )
        state: Any = {
            "trigger_event": pull_request.model_dump(),
            "workspace_id": "",
            "project_policy": LoadedPolicy(policy=POLICY, sha256="0" * 64),
            "unmapped_files": ["docs/guide.txt", "Makefile"],
        }

        result = coordinator_nodes.extract_trigger_node(state)

        self.assertEqual(result["unmapped_files"], ["Makefile"])
        exempted = result["exempted_files"]
        assert isinstance(exempted, list)
        self.assertEqual([e.subject for e in exempted], ["docs/guide.txt"])
        issues = result["discovery_issues"]
        assert isinstance(issues, list)
        self.assertEqual([issue.code for issue in issues], [ReasonCode.UNMAPPED_FILES])

    def test_telemetry_node_classifies_runtime_evidence(self) -> None:
        loaded = LoadedPolicy(policy=POLICY, sha256="0" * 64)
        baseline_only: Any = {
            "workspace_id": "",
            "project_policy": loaded,
            "baseline_runtime_state": RuntimeEvidenceState.BASELINE,
        }

        result = coordinator_nodes.validate_runtime_telemetry_node(baseline_only)

        status = result["telemetry_status"]
        self.assertIs(status.runtime_evidence, RuntimeEvidenceState.BASELINE)
        issues = result["discovery_issues"]
        assert isinstance(issues, list)
        self.assertEqual(
            [issue.code for issue in issues], [ReasonCode.REQUIRED_SOURCE_MISSING]
        )

        cases = [
            (DiscoveryResult.success([]), False, RuntimeEvidenceState.LIVE),
            (DiscoveryResult.failed("x"), False, RuntimeEvidenceState.LIVE_INCOMPLETE),
            (DiscoveryResult.success([]), True, RuntimeEvidenceState.MOCK),
        ]
        for result_value, is_mock, expected in cases:
            with self.subTest(expected), patch.object(
                coordinator_nodes, "TelemetryExtractor"
            ) as extractor_cls:
                extractor = extractor_cls.return_value
                extractor.is_mock = is_mock
                extractor.get_active_dependencies.return_value = result_value
                extractor.get_runtime_metrics.return_value = {}
                live: Any = {"workspace_id": "ws", "project_policy": loaded}

                result = coordinator_nodes.validate_runtime_telemetry_node(live)

                self.assertIs(result["telemetry_status"].runtime_evidence, expected)
                codes = [issue.code for issue in result["discovery_issues"]]
                self.assertEqual(
                    ReasonCode.REQUIRED_SOURCE_MISSING in codes,
                    expected is not RuntimeEvidenceState.LIVE,
                )

    def test_policy_type_is_frozen(self) -> None:
        self.assertIsInstance(POLICY, ProjectPolicy)
        with self.assertRaises(ValidationError):
            POLICY.policy_version = 9  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()

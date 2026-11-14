"""Structured evidence issues and their grouping keys (step 2a)."""

import unittest

from src.coordinator.coverage import static_coverage_issues
from src.core.issue_keys import files_key, identity_key, unresolved_key
from src.core.models import (
    EvidenceIssue,
    IssueSubjectKind,
    KnowledgeGraph,
    ReasonCode,
    RepositoryCoverage,
    ServiceCoverage,
    UnscannedFile,
    UnscannedReason,
)


class KeyTests(unittest.TestCase):
    def test_env_variables_group_by_name_across_services(self) -> None:
        self.assertEqual(
            unresolved_key("env:basket-api/OTEL_EXPORTER_OTLP_ENDPOINT", "unresolved:env:basket-api/otel"),
            unresolved_key("env:webapp/OTEL_EXPORTER_OTLP_ENDPOINT", "unresolved:env:webapp/otel"),
        )
        self.assertEqual(
            unresolved_key("env:webapp/OTEL_EXPORTER_OTLP_ENDPOINT", "x"), "env:otel_exporter_otlp_endpoint"
        )

    def test_addresses_group_by_host(self) -> None:
        self.assertEqual(unresolved_key("https://10.0.2.2:5243", "a"), "host:10.0.2.2")
        self.assertEqual(unresolved_key("10.0.2.2:80", "b"), "host:10.0.2.2")
        self.assertEqual(unresolved_key("", "unresolved:x"), "entity:unresolved:x")

    def test_identical_file_sets_share_a_key(self) -> None:
        library = ["src/EventBus/Bus.cs", "src/Defaults/Config.cs"]
        self.assertEqual(files_key("eShop", library), files_key("eShop", list(reversed(library))))
        self.assertNotEqual(files_key("eShop", library), files_key("eShop", library[:1]))
        self.assertNotEqual(files_key("eShop", library), files_key("other", library))
        self.assertEqual(identity_key("WebhookClient"), "identity:webhookclient")


class IssueModelTests(unittest.TestCase):
    def test_root_key_defaults_to_the_code_and_old_issues_load(self) -> None:
        old = EvidenceIssue.model_validate({"code": "BASELINE_STALE", "detail": "too old"})
        self.assertEqual(old.root_key, "BASELINE_STALE")
        self.assertEqual((old.subjects, old.entity_kind, old.via), ([], None, []))


class CoverageIssueTests(unittest.TestCase):
    def test_consumers_of_one_library_share_the_cause(self) -> None:
        site = [UnscannedFile(path="src/EventBus/Bus.cs", reason=UnscannedReason.REFERENCES_NOT_UNDERSTOOD)]
        coverage = RepositoryCoverage(
            repository="eShop",
            commit_sha="a" * 40,
            services=[
                ServiceCoverage(service_name="basket-api", root="src/Basket.API", unscanned=site),
                ServiceCoverage(service_name="ordering-api", root="src/Ordering.API", unscanned=site),
            ],
        )
        graph = KnowledgeGraph(services=[], edges=[], static_coverage=[coverage])
        issues, _ = static_coverage_issues(None, graph, "snap")
        self.assertEqual([i.code for i in issues], [ReasonCode.STATIC_COVERAGE_INCOMPLETE] * 2)
        self.assertEqual(len({i.root_key for i in issues}), 1)
        self.assertEqual(
            [(s.kind, s.id) for s in issues[0].subjects],
            [(IssueSubjectKind.SERVICE, "basket-api"), (IssueSubjectKind.FILE, "eShop:src/EventBus/Bus.cs")],
        )


if __name__ == "__main__":
    unittest.main()

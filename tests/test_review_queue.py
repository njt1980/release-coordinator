"""Tests for the review queue (T4d): weakest evidence first, with owners."""

import unittest
from typing import Any

from src.core.models import DiscoveryMethod, ReasonCode
from src.core.review import ReviewSubject, ReviewSubjectKind, StaleReason
from src.orchestration.review import DependencyRow, ProjectReview, QuestionRow, ReviewStatus
from src.orchestration.review_queue import review_queue
from src.tools.baseline_store import BaselineScope
from src.tools.policy_store import PolicyInvalidError, parse_policy

URN = "urn:release-coord:acme:prod:aks-1:shop"


def _id(name: str) -> str:
    return f"{URN}:{name}"


def _dependency(source: str, target: str, methods: list[DiscoveryMethod], **fields: Any) -> DependencyRow:
    values: dict[str, Any] = dict(status=ReviewStatus.OPEN, provenance="x")
    values.update(fields)
    return DependencyRow(
        subject=ReviewSubject(kind=ReviewSubjectKind.DEPENDENCY, key=f"edge:{source}->{target}"),
        source_id=_id(source), target_id=_id(target), discovery_methods=methods, **values,
    )


def _question(key: str, services: list[str], **fields: Any) -> QuestionRow:
    values: dict[str, Any] = dict(status=ReviewStatus.OPEN)
    values.update(fields)
    return QuestionRow(
        subject=ReviewSubject(kind=ReviewSubjectKind.QUESTION, key=key), code=ReasonCode.DISCOVERY_GAP,
        details=[f"detail of {key}"], services=[_id(s) for s in services], **values,
    )


CODE, RUNTIME = DiscoveryMethod.STATIC_CODE, DiscoveryMethod.RUNTIME_TELEMETRY
REVIEW = ProjectReview(
    snapshot_id="snap",
    services=[],
    dependencies=[
        _dependency("front", "orders", [CODE, RUNTIME]),
        _dependency("orders", "db", [CODE]),
        _dependency("admin", "db", [CODE]),
        _dependency("orders", "queue", [CODE], weak="only a default value in source names it"),
        _dependency("orders", "cache", [CODE], status=ReviewStatus.CONFIRMED),
        _dependency("front", "cdn", [RUNTIME], status=ReviewStatus.NEEDS_REREVIEW,
                    stale=[StaleReason(repository="shop", detail="src/front/api.js changed")]),
    ],
    questions=[
        _question("question:a", ["front"]),
        _question("question:b", ["orders"], status=ReviewStatus.ANSWERED),
    ],
)
# db is the most critical service, then orders, then front.
RANKS = {_id("db"): 1, _id("orders"): 2, _id("front"): 3, _id("admin"): 4, _id("queue"): 5, _id("cdn"): 6}


def _owners(service: str) -> str | None:
    return {"shop/orders": "team-orders", "shop/front": "team-web"}.get(service)


class ReviewQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = review_queue(REVIEW, RANKS, _owners)

    def test_weakest_evidence_first(self) -> None:
        self.assertEqual(
            [(item.position, item.level, item.title) for item in self.queue],
            [
                (1, 1, "front → cdn"),
                (2, 2, "Unresolved question:a in front"),
                (3, 3, "orders → queue"),
                # Same level: the one touching the more critical service first.
                (4, 4, "orders → db"),
                (5, 4, "admin → db"),
                (6, 5, "front → orders"),
            ],
        )

    def test_answered_items_leave_the_queue(self) -> None:
        keys = {item.key for item in self.queue}
        self.assertNotIn("edge:orders->cache", keys)
        self.assertNotIn("question:b", keys)

    def test_reasons_and_owners(self) -> None:
        by_title = {item.title: item for item in self.queue}
        self.assertEqual(by_title["front → cdn"].why, "Its earlier answer stopped counting: src/front/api.js changed.")
        self.assertEqual(by_title["orders → db"].why, "One source only: code.")
        self.assertEqual(by_title["front → orders"].why, "code and telemetry agree.")
        # A dependency belongs to its caller; admin has no owner recorded.
        self.assertEqual(by_title["orders → db"].owners, ["team-orders"])
        self.assertEqual(by_title["admin → db"].owners, [])
        self.assertEqual(by_title["Unresolved question:a in front"].owners, ["team-web"])


class QuestionTitleTests(unittest.TestCase):
    def test_titles_name_the_subject_and_services(self) -> None:
        row = _question("env:otel_exporter_otlp_endpoint", ["a", "b", "c", "d", "e"])
        self.assertEqual(
            review_queue(ProjectReview(snapshot_id="s", services=[], dependencies=[], questions=[row]), {}, _owners)[0].title,
            "Unresolved variable otel_exporter_otlp_endpoint in a, b, c and 2 more",
        )


class OwnerPolicyTests(unittest.TestCase):
    def _policy(self, extra: str) -> Any:
        return parse_policy(
            "policy_version: 1\ntenant: acme\nproject_id: shop\nenvironment: prod\ncluster: aks-1\n"
            "required_sources: [STATIC_CODE]\nruntime_evidence: baseline\n" + extra,
            BaselineScope(tenant="acme", project_id="shop"),
        )

    def test_owners_by_namespaced_name(self) -> None:
        policy = self._policy("service_owners:\n  - service: Shop/Orders\n    owner: team-orders\n")
        self.assertEqual(policy.owner_of("shop/orders"), "team-orders")
        self.assertIsNone(policy.owner_of("shop/front"))
        with self.assertRaises(PolicyInvalidError):
            self._policy("service_owners:\n  - service: orders\n    owner: x\n")


if __name__ == "__main__":
    unittest.main()

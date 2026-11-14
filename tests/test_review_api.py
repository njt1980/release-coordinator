"""Project dependency review API."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import api
from src.coordinator import nodes as coordinator_nodes
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    KnowledgeGraph,
    UnresolvedEntity,
    UnresolvedEntityKind,
)
from src.core.review import edge_key
from src.tools.baseline_store import BaselineScope, BaselineStore
from src.tools.policy_store import PolicyStore
from src.tools.review_store import ReviewStore
from tests.test_pr_webhook import BASE, POLICY_YAML, SCAN, SERVICES, _id

URL = "/api/v1/projects/acme/shop/review"
CONFIG = "unresolved:config-server"


def _edge(source: str, target: str, files: list[str]) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[DiscoveryMethod.STATIC_CODE],
        confidence_score=0.7,
        last_observed_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        evidence_files=files,
    )


class ReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        base = Path(root.name)
        scope = BaselineScope(tenant="acme", project_id="shop")
        baselines = BaselineStore(base / "baselines")
        graph = KnowledgeGraph(
            services=SERVICES,
            edges=[
                _edge(_id("store-front"), _id("order-service"), ["src/store-front/api.js"]),
                _edge(_id("store-front"), CONFIG, ["src/store-front/boot.js"]),
            ],
            unresolved_entities=[
                UnresolvedEntity(entity_id=CONFIG, kind=UnresolvedEntityKind.PRIVATE_DNS, reference="config-server")
            ],
            discovery=[DiscoveryStageReport(source=m, status=DiscoveryStatus.SUCCESS) for m in DiscoveryMethod],
            scan=SCAN,
        )
        self.snapshot = baselines.write(scope, graph).snapshot_id
        policies = PolicyStore(base / "policies")
        path = policies.policy_path(scope)
        path.parent.mkdir(parents=True)
        path.write_text(POLICY_YAML, encoding="utf-8")
        self.reviews = ReviewStore(base / "reviews.sqlite3")
        for name, value in (("BASELINE_STORE", baselines), ("POLICY_STORE", policies), ("REVIEW_STORE", self.reviews)):
            patcher = patch.object(coordinator_nodes, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app)
        self.edge = edge_key(_id("store-front"), _id("order-service"))

    def _post(self, body: dict[str, Any], **headers: str) -> Any:
        return self.client.post(f"{URL}/answers", json=body, headers=headers)

    def _row(self, review: dict[str, Any], key: str) -> dict[str, Any]:
        return next(row for row in review["dependencies"] if row["subject"]["key"] == key)

    def test_review_lists_dependencies_and_questions(self) -> None:
        review = self.client.get(URL).json()
        self.assertEqual(review["snapshot_id"], self.snapshot)
        row = self._row(review, self.edge)
        self.assertEqual((row["status"], row["evidence_files"]), ("open", ["src/store-front/api.js"]))
        self.assertEqual(row["anchors"][0]["paths"], ["src/store-front/api.js"])
        (question,) = review["questions"]
        self.assertEqual(question["subject"]["key"], "host:config-server")
        self.assertEqual(question["anchors"][0]["paths"], ["src/store-front/"])

    def test_answers_are_anchored_by_the_server(self) -> None:
        response = self._post(
            {"subject": {"kind": "dependency", "key": self.edge}, "verdict": "disagree", "reviewer": "Dana", "reason": "dead code"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self._row(response.json(), self.edge)["status"], "removed")
        (stored,) = self.reviews.answers(BaselineScope(tenant="acme", project_id="shop"))
        self.assertEqual(stored.anchors[0].commit_sha, BASE)
        self.assertEqual(stored.snapshot_id, self.snapshot)
        self.assertIn("file:src/store-front/api.js", stored.evidence_seen)

    def test_signed_in_user_is_the_reviewer(self) -> None:
        self._post(
            {"subject": {"kind": "dependency", "key": self.edge}, "verdict": "agree", "reviewer": "anyone"},
            **{"X-MS-CLIENT-PRINCIPAL-NAME": "dana@contoso.com"},
        )
        (stored,) = self.reviews.answers(BaselineScope(tenant="acme", project_id="shop"))
        self.assertEqual(stored.reviewer, "dana@contoso.com")

    def test_invalid_answers_are_rejected(self) -> None:
        cases = {
            "no reason": {"subject": {"kind": "dependency", "key": self.edge}, "verdict": "disagree", "reviewer": "D"},
            "no reviewer": {"subject": {"kind": "dependency", "key": self.edge}, "verdict": "agree"},
            "unknown edge": {"subject": {"kind": "dependency", "key": edge_key("a", "b")}, "verdict": "agree", "reviewer": "D"},
            "unknown question": {"subject": {"kind": "question", "key": "host:x"}, "verdict": "nothing_hidden", "reviewer": "D", "reason": "r"},
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual(self._post(body).status_code, 422)
        self.assertEqual(self.reviews.answers(BaselineScope(tenant="acme", project_id="shop")), [])

    def test_add_and_export(self) -> None:
        source, target = _id("payments-api"), _id("order-service")
        response = self._post(
            {
                "subject": {"kind": "dependency", "key": edge_key(source, target)},
                "verdict": "add",
                "reviewer": "Dana",
                "reason": "gateway route",
                "declared": [{"source_id": source, "target_id": target}],
            }
        )
        self.assertEqual(response.status_code, 201, response.text)
        row = self._row(response.json(), edge_key(source, target))
        self.assertEqual((row["status"], row["anchors"][0]["paths"]), ("added", ["services/payments/api/"]))
        self._post(
            {"subject": {"kind": "question", "key": "host:config-server"}, "verdict": "nothing_hidden", "reviewer": "Dana", "reason": "dev only"}
        )
        export = self.client.get(f"{URL}/export").json()
        self.assertEqual(sorted(c["kind"] for c in export["corrections"]), ["false_alarm", "missed"])

    def test_unknown_project_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/v1/projects/acme/none/review").status_code, 404)


if __name__ == "__main__":
    unittest.main()

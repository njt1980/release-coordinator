"""Dependency review: answers in force, the confirmed map, the review page."""

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.issue_keys import identity_key, unresolved_key
from src.core.models import (
    IDENTITY_UNLINKED_METADATA_KEY,
    DependencyEdge,
    DiscoveryMethod,
    EvidenceIssue,
    IssueSubject,
    IssueSubjectKind,
    KnowledgeGraph,
    ReasonCode,
    ScanContext,
    ScannedRevision,
    ServiceNode,
    StaticDefaultOrigin,
    StaticDefaultUse,
    UnresolvedEntity,
    UnresolvedEntityKind,
)
from src.core.review import (
    DeclaredDependency,
    RepositoryChanges,
    ReviewAnchor,
    ReviewAnswer,
    ReviewSubject,
    ReviewSubjectKind,
    ReviewVerdict,
    edge_evidence,
    edge_key,
)
from src.orchestration.review import (
    ReviewStatus,
    answers_in_force,
    build_review,
    confirmed_map,
    edge_anchors,
    issue_anchors,
    split_accepted,
)

URN = "urn:release-coord:acme:prod:aks-1:shop"
OLD, NEW = "a" * 40, "b" * 40
T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
CHECKOUT, PAYMENT, ORDERS = f"{URN}:checkout", f"{URN}:payment", f"{URN}:orders"
HOOKS_SRC, HOOKS_RUN = f"{URN}:webhookclient-src", f"{URN}:webhooks-client"
CONFIG = "unresolved:config-server"


def _node(service_id: str, name: str, root: str | None = None, **metadata: str) -> ServiceNode:
    if root is not None:
        metadata = {"repository": "shop", "service_root": root, **metadata}
    return ServiceNode(service_id=service_id, name=name, metadata=metadata)


def _edge(source: str, target: str, *methods: DiscoveryMethod, files: list[str] | None = None, **extra: Any) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=list(methods or [DiscoveryMethod.STATIC_CODE]),
        confidence_score=0.7,
        last_observed_at=T0,
        evidence_files=files,
        **extra,
    )


def _graph(**overrides: Any) -> KnowledgeGraph:
    fields: dict[str, Any] = {
        "services": [
            _node(CHECKOUT, "checkout", "src/checkout"),
            _node(PAYMENT, "payment", "src/payment"),
            _node(ORDERS, "orders", ""),
            _node(HOOKS_SRC, "webhookclient", "src/hooks", **{IDENTITY_UNLINKED_METADATA_KEY: "true"}),
            _node(HOOKS_RUN, "webhooks-client"),
        ],
        "edges": [
            _edge(CHECKOUT, PAYMENT, files=["src/checkout/pay.py"]),
            _edge(ORDERS, PAYMENT, DiscoveryMethod.K8S_MANIFEST, provenance="kubernetes_env", files=[]),
            _edge(HOOKS_SRC, ORDERS, files=["src/hooks/a.cs"]),
            _edge(PAYMENT, HOOKS_RUN, DiscoveryMethod.RUNTIME_TELEMETRY, files=[]),
            _edge(CHECKOUT, CONFIG, files=["src/checkout/boot.py"]),
        ],
        "unresolved_entities": [
            UnresolvedEntity(entity_id=CONFIG, kind=UnresolvedEntityKind.PRIVATE_DNS, reference="config-server")
        ],
        "scan": ScanContext(
            environment="prod",
            cluster="aks-1",
            scanned_revisions=[ScannedRevision(repository="shop", branch="main", commit_sha=OLD)],
        ),
    }
    fields.update(overrides)
    return KnowledgeGraph(**fields)


def _dependency(source: str, target: str) -> ReviewSubject:
    return ReviewSubject(kind=ReviewSubjectKind.DEPENDENCY, key=edge_key(source, target))


def _answer(subject: ReviewSubject, verdict: ReviewVerdict, answer_id: str = "a1", **fields: Any) -> ReviewAnswer:
    defaults: dict[str, Any] = {
        "answer_id": answer_id,
        "subject": subject,
        "verdict": verdict,
        "reviewer": "Dana",
        "answered_at": T0,
        "snapshot_id": "snap-1",
        "anchors": (ReviewAnchor(repository="shop", commit_sha=OLD, paths=("src/checkout/pay.py",)),),
    }
    if verdict not in (ReviewVerdict.AGREE, ReviewVerdict.LINK):
        defaults["reason"] = "runbook"
    defaults.update(fields)
    return ReviewAnswer(**defaults)


def _config_issue() -> EvidenceIssue:
    return EvidenceIssue(
        code=ReasonCode.DISCOVERY_GAP,
        detail="config-server is downstream via checkout",
        subjects=[IssueSubject(kind=IssueSubjectKind.ENTITY, id=CONFIG)],
        root_key=unresolved_key("config-server", CONFIG),
        via=[CHECKOUT, CONFIG],
    )


def _in_force(graph: KnowledgeGraph, *answers: ReviewAnswer, commit: str = OLD, **kwargs: Any) -> Any:
    return answers_in_force(graph, answers, {"shop": commit}, kwargs.pop("changes", []), **kwargs)


class AnchorTests(unittest.TestCase):
    def test_cited_files_root_fallback_and_whole_repository(self) -> None:
        graph = _graph()
        (cited,) = edge_anchors(graph, graph.edges[0]) or ()
        self.assertEqual((cited.commit_sha, cited.paths), (OLD, ("src/checkout/pay.py",)))
        (fallback,) = edge_anchors(graph, graph.edges[1]) or ()
        self.assertEqual(fallback.paths, ("/",))  # orders lives at the repository root
        (telemetry,) = edge_anchors(graph, graph.edges[3]) or ()
        self.assertEqual(telemetry.paths, ("src/payment/",))

    def test_static_default_file_joins_the_anchor(self) -> None:
        use = StaticDefaultUse(
            service_id=CHECKOUT, variable="PAY", endpoint="http://payment", origin=StaticDefaultOrigin.CONFIG_FILE,
            file_path="/src/checkout/appsettings.json", target=PAYMENT,
        )
        graph = _graph(static_defaults_used=[use])
        (anchor,) = edge_anchors(graph, graph.edges[0]) or ()
        self.assertEqual(anchor.paths, ("src/checkout/appsettings.json", "src/checkout/pay.py"))

    def test_no_repository_means_unanswerable(self) -> None:
        graph = _graph()
        self.assertIsNone(edge_anchors(graph, _edge(HOOKS_RUN, PAYMENT, DiscoveryMethod.RUNTIME_TELEMETRY)))

    def test_question_anchors(self) -> None:
        graph = _graph()
        (callers,) = issue_anchors(graph, [_config_issue()]) or ()
        self.assertEqual(callers.paths, ("src/checkout/",))
        unread = EvidenceIssue(
            code=ReasonCode.STATIC_COVERAGE_INCOMPLETE, detail="x", root_key="unscanned:shop:1",
            subjects=[IssueSubject(kind=IssueSubjectKind.FILE, id="shop:src/payment/Gen.cs")],
        )
        (files,) = issue_anchors(graph, [unread]) or ()
        self.assertEqual(files.paths, ("src/payment/Gen.cs",))


class InForceTests(unittest.TestCase):
    def test_fresh_answers_count(self) -> None:
        agree = _answer(_dependency(CHECKOUT, PAYMENT), ReviewVerdict.AGREE)
        self.assertEqual(list(_in_force(_graph(), agree).answers.values()), [agree])

    def test_anchored_change_makes_it_stale(self) -> None:
        agree = _answer(_dependency(CHECKOUT, PAYMENT), ReviewVerdict.AGREE)
        changes = [RepositoryChanges(repository="shop", from_commit=OLD, to_commit=NEW, from_is_ancestor=True, changed_paths=("src/checkout/pay.py",))]
        in_force = _in_force(_graph(), agree, commit=NEW, changes=changes)
        self.assertEqual(in_force.answers, {})
        self.assertIs(in_force.stale[agree.subject].answer, agree)

    def test_pull_request_changes_count_too(self) -> None:
        agree = _answer(_dependency(CHECKOUT, PAYMENT), ReviewVerdict.AGREE)
        in_force = _in_force(_graph(), agree, pending_changes={"shop": ["src/checkout/pay.py"]})
        self.assertIn("this pull request", in_force.stale[agree.subject].reasons[0].detail)

    def test_disagreed_edge_returns_with_new_evidence(self) -> None:
        graph = _graph()
        subject = _dependency(CHECKOUT, PAYMENT)
        seen = edge_evidence(graph.edges[0])
        disagree = _answer(subject, ReviewVerdict.DISAGREE, evidence_seen=seen)
        self.assertIn(subject, _in_force(graph, disagree).answers)
        grown = graph.model_copy(update={"edges": [_edge(CHECKOUT, PAYMENT, DiscoveryMethod.STATIC_CODE, DiscoveryMethod.RUNTIME_TELEMETRY, files=["src/checkout/pay.py"]), *graph.edges[1:]]})
        stale = _in_force(grown, disagree).stale[subject]
        self.assertIn("method:RUNTIME_TELEMETRY", stale.reasons[0].detail)


class ConfirmedMapTests(unittest.TestCase):
    def _pairs(self, graph: KnowledgeGraph) -> set[tuple[str, str]]:
        return {(e.source_service_id, e.target_service_id) for e in graph.edges}

    def test_disagree_removes_and_add_declares(self) -> None:
        graph = _graph()
        disagree = _answer(_dependency(ORDERS, PAYMENT), ReviewVerdict.DISAGREE, "a1", evidence_seen=edge_evidence(graph.edges[1]))
        declared = DeclaredDependency(source_id=CHECKOUT, target_id=ORDERS, protocol="grpc")
        add = _answer(_dependency(CHECKOUT, ORDERS), ReviewVerdict.ADD, "a2", declared=(declared,))
        result = confirmed_map(graph, _in_force(graph, disagree, add))
        pairs = self._pairs(result.graph)
        self.assertNotIn((ORDERS, PAYMENT), pairs)
        self.assertIn((CHECKOUT, ORDERS), pairs)
        added = next(e for e in result.graph.edges if (e.source_service_id, e.target_service_id) == (CHECKOUT, ORDERS))
        self.assertEqual((added.discovery_methods, added.provenance, added.confidence_score), ([DiscoveryMethod.HUMAN_DECLARED], "human_declared", 0.0))
        self.assertEqual([a.edges for a in result.removed], [[edge_key(ORDERS, PAYMENT)]])

    def test_declaring_a_found_edge_merges_evidence(self) -> None:
        graph = _graph()
        declared = DeclaredDependency(source_id=CHECKOUT, target_id=PAYMENT)
        add = _answer(_dependency(CHECKOUT, PAYMENT), ReviewVerdict.ADD, declared=(declared,))
        edge = next(e for e in confirmed_map(graph, _in_force(graph, add)).graph.edges if e.target_service_id == PAYMENT and e.source_service_id == CHECKOUT)
        self.assertEqual(edge.discovery_methods, [DiscoveryMethod.STATIC_CODE, DiscoveryMethod.HUMAN_DECLARED])
        self.assertEqual(edge.evidence_files, ["src/checkout/pay.py"])

    def test_questions_accepted_only_when_fully_applied(self) -> None:
        graph = _graph()
        question = ReviewSubject(kind=ReviewSubjectKind.QUESTION, key=_config_issue().root_key)
        hidden = _answer(question, ReviewVerdict.NOTHING_HIDDEN)
        self.assertIn(question.key, confirmed_map(graph, _in_force(graph, hidden)).accepted)
        calls = _answer(question, ReviewVerdict.CALLS, declared=(DeclaredDependency(source_id=CHECKOUT, target_id=f"{URN}:vault"),))
        result = confirmed_map(graph, _in_force(graph, calls))
        self.assertNotIn(question.key, result.accepted)
        self.assertIn("not in the baseline", result.unapplied[0])

    def test_link_merges_identities(self) -> None:
        graph = _graph()
        subject = ReviewSubject(kind=ReviewSubjectKind.IDENTITY, key=identity_key("webhookclient"))
        link = _answer(subject, ReviewVerdict.LINK, linked_runtime_name="webhooks-client", anchors=())
        result = confirmed_map(graph, _in_force(graph, link))
        self.assertEqual(result.renamed, {HOOKS_SRC: HOOKS_RUN})
        self.assertEqual(self._pairs(result.graph) & {(HOOKS_RUN, ORDERS), (PAYMENT, HOOKS_RUN)}, {(HOOKS_RUN, ORDERS), (PAYMENT, HOOKS_RUN)})
        merged = next(s for s in result.graph.services if s.service_id == HOOKS_RUN)
        self.assertEqual(merged.metadata["service_root"], "src/hooks")
        self.assertNotIn(HOOKS_SRC, {s.service_id for s in result.graph.services})

    def test_split_accepted_ignores_catch_all_keys(self) -> None:
        answer = _answer(ReviewSubject(kind=ReviewSubjectKind.QUESTION, key="DISCOVERY_GAP"), ReviewVerdict.NOTHING_HIDDEN)
        generic = EvidenceIssue(code=ReasonCode.DISCOVERY_GAP, detail="target unresolved")
        specific = _config_issue()
        remaining, covered = split_accepted(
            [generic, specific], {"DISCOVERY_GAP": answer, specific.root_key: answer}
        )
        self.assertEqual(remaining, [generic])
        self.assertEqual([c.issue for c in covered], [specific])


class BuildReviewTests(unittest.TestCase):
    def test_rows_and_statuses(self) -> None:
        graph = _graph()
        agree = _answer(_dependency(CHECKOUT, PAYMENT), ReviewVerdict.AGREE, "a1")
        add = _answer(
            _dependency(PAYMENT, ORDERS), ReviewVerdict.ADD, "a2",
            declared=(DeclaredDependency(source_id=PAYMENT, target_id=ORDERS),),
            answered_at=T0 + timedelta(minutes=1),
        )
        review = build_review(graph, "snap-1", [_config_issue()], _in_force(graph, agree, add))
        by_key = {row.subject.key: row for row in review.dependencies}
        self.assertEqual(by_key[edge_key(CHECKOUT, PAYMENT)].status, ReviewStatus.CONFIRMED)
        self.assertEqual(by_key[edge_key(PAYMENT, ORDERS)].status, ReviewStatus.ADDED)
        self.assertEqual(by_key[edge_key(ORDERS, PAYMENT)].weak, "only a Kubernetes environment variable names it (it may be unused)")
        self.assertEqual(by_key[edge_key(ORDERS, PAYMENT)].status, ReviewStatus.OPEN)
        (question,) = review.questions
        self.assertEqual((question.status, question.services), (ReviewStatus.OPEN, [CHECKOUT]))
        self.assertEqual(len(review.services), 5)


if __name__ == "__main__":
    unittest.main()

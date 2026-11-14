"""Project dependency review: answer records, staleness, and the store."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.core.issue_keys import identity_key
from src.core.review import (
    DeclaredDependency,
    RepositoryChanges,
    ReviewAnchor,
    ReviewAnswer,
    ReviewSubject,
    ReviewSubjectKind,
    ReviewVerdict,
    answer_freshness,
    current_answers,
    edge_key,
)
from src.tools.baseline_store import BaselineScope
from src.tools.review_store import (
    DuplicateAnswerError,
    ReviewStore,
    ReviewStoreCorruptError,
)

OLD = "a" * 40
NEW = "b" * 40
T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
CHECKOUT = "urn:svc:default/prod/aks/shop/checkout"
PAYMENT = "urn:svc:default/prod/aks/shop/payment"
EDGE = ReviewSubject(kind=ReviewSubjectKind.DEPENDENCY, key=edge_key(CHECKOUT, PAYMENT))
QUESTION = ReviewSubject(kind=ReviewSubjectKind.QUESTION, key="env:payment_url")


def _anchor(*paths: str, commit: str = OLD, repository: str = "shop") -> ReviewAnchor:
    return ReviewAnchor(repository=repository, commit_sha=commit, paths=paths)


def _answer(**overrides: Any) -> ReviewAnswer:
    fields: dict[str, Any] = {
        "answer_id": "a1",
        "subject": EDGE,
        "verdict": ReviewVerdict.AGREE,
        "reviewer": "  Dana  ",
        "answered_at": T0,
        "snapshot_id": "20261114T090000000000Z-abc",
        "anchors": (_anchor("checkout/app.py"),),
    }
    fields.update(overrides)
    return ReviewAnswer(**fields)


def _changes(*paths: str, ancestor: bool = True) -> RepositoryChanges:
    return RepositoryChanges(
        repository="shop",
        from_commit=OLD,
        to_commit=NEW,
        from_is_ancestor=ancestor,
        changed_paths=paths,
    )


class AnswerValidationTests(unittest.TestCase):
    def test_agree_needs_no_reason_and_reviewer_is_trimmed(self) -> None:
        self.assertEqual(_answer().reviewer, "Dana")

    def test_verdict_must_fit_the_subject(self) -> None:
        with self.assertRaisesRegex(ValidationError, "not an answer"):
            _answer(verdict=ReviewVerdict.NOTHING_HIDDEN, reason="none")
        with self.assertRaisesRegex(ValidationError, "not an answer"):
            _answer(subject=QUESTION)

    def test_reason_required_to_disagree_add_and_answer(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires a reason"):
            _answer(verdict=ReviewVerdict.DISAGREE, evidence_seen=("method:STATIC_CODE",), reason="   ")
        self.assertEqual(
            _answer(verdict=ReviewVerdict.DISAGREE, evidence_seen=("method:STATIC_CODE",), reason=" dead code ").reason,
            "dead code",
        )
        with self.assertRaisesRegex(ValidationError, "requires a reason"):
            _answer(subject=QUESTION, verdict=ReviewVerdict.NOTHING_HIDDEN)

    def test_add_declares_exactly_its_own_edge(self) -> None:
        declared = DeclaredDependency(source_id=CHECKOUT, target_id=PAYMENT, protocol="grpc")
        added = _answer(verdict=ReviewVerdict.ADD, reason="runbook", declared=(declared,))
        self.assertEqual(added.declared, (declared,))
        other = DeclaredDependency(source_id=PAYMENT, target_id=CHECKOUT)
        with self.assertRaisesRegex(ValidationError, "exactly the dependency"):
            _answer(verdict=ReviewVerdict.ADD, reason="runbook", declared=(other,))
        with self.assertRaisesRegex(ValidationError, "requires declared"):
            _answer(verdict=ReviewVerdict.ADD, reason="runbook")
        with self.assertRaisesRegex(ValidationError, "declares no"):
            _answer(declared=(declared,))

    def test_calls_declares_unique_dependencies(self) -> None:
        declared = DeclaredDependency(source_id=CHECKOUT, target_id=PAYMENT)
        answer = _answer(
            subject=QUESTION, verdict=ReviewVerdict.CALLS, reason="config server", declared=(declared,)
        )
        self.assertEqual(len(answer.declared), 1)
        with self.assertRaisesRegex(ValidationError, "unique"):
            _answer(
                subject=QUESTION,
                verdict=ReviewVerdict.CALLS,
                reason="config server",
                declared=(declared, declared),
            )

    def test_self_dependency_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "itself"):
            DeclaredDependency(source_id=CHECKOUT, target_id=CHECKOUT)

    def test_link_needs_runtime_name_and_no_anchors(self) -> None:
        identity = ReviewSubject(kind=ReviewSubjectKind.IDENTITY, key=identity_key("webhookclient"))
        link = _answer(
            subject=identity,
            verdict=ReviewVerdict.LINK,
            linked_runtime_name="webhooks-client",
            anchors=(),
        )
        self.assertEqual(link.linked_runtime_name, "webhooks-client")
        with self.assertRaisesRegex(ValidationError, "exactly for link"):
            _answer(subject=identity, verdict=ReviewVerdict.LINK, anchors=())
        with self.assertRaisesRegex(ValidationError, "exactly for link"):
            _answer(linked_runtime_name="webhooks-client")

    def test_anchors_required_and_valid(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires anchors"):
            _answer(anchors=())
        with self.assertRaisesRegex(ValidationError, "once"):
            _answer(anchors=(_anchor("a.py"), _anchor("b.py")))
        for bad in ("/abs.py", "a/../b.py", "a//b.py", "a\\b.py", ""):
            with self.subTest(path=bad), self.assertRaises(ValidationError):
                _anchor(bad)
        with self.assertRaises(ValidationError):
            _anchor("a.py", commit="ABC")
        with self.assertRaises(ValidationError):
            _anchor()

    def test_disagree_records_the_evidence_seen(self) -> None:
        with self.assertRaisesRegex(ValidationError, "evidence_seen"):
            _answer(verdict=ReviewVerdict.DISAGREE, reason="dead code")
        with self.assertRaisesRegex(ValidationError, "only a dependency"):
            _answer(subject=QUESTION, verdict=ReviewVerdict.NOTHING_HIDDEN, reason="x", evidence_seen=("a",))

    def test_subject_keys(self) -> None:
        with self.assertRaisesRegex(ValidationError, "edge key"):
            ReviewSubject(kind=ReviewSubjectKind.DEPENDENCY, key="env:x")
        with self.assertRaisesRegex(ValidationError, "edge key"):
            ReviewSubject(kind=ReviewSubjectKind.QUESTION, key=edge_key("a", "b"))

    def test_naive_timestamps_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _answer(answered_at=datetime(2026, 11, 14, 9, 0))


class CurrentAnswerTests(unittest.TestCase):
    def test_newest_answer_per_subject_wins(self) -> None:
        agree = _answer()
        disagree = _answer(
            answer_id="a2",
            verdict=ReviewVerdict.DISAGREE, evidence_seen=("method:STATIC_CODE",),
            reason="removed in v2",
            answered_at=T0 + timedelta(hours=1),
        )
        question = _answer(
            answer_id="a3", subject=QUESTION, verdict=ReviewVerdict.NOTHING_HIDDEN, reason="unused"
        )
        current = current_answers([disagree, question, agree])
        self.assertEqual(current, {EDGE: disagree, QUESTION: question})

    def test_ties_broken_by_answer_id(self) -> None:
        first, second = _answer(answer_id="a1"), _answer(answer_id="a2")
        self.assertIs(current_answers([second, first])[EDGE], second)


class FreshnessTests(unittest.TestCase):
    def test_same_commit_is_current(self) -> None:
        freshness = answer_freshness(_answer(), {"shop": OLD}, [])
        self.assertFalse(freshness.needs_rereview)

    def test_change_to_anchored_file_needs_rereview(self) -> None:
        freshness = answer_freshness(
            _answer(), {"shop": NEW}, [_changes("checkout/app.py", "docs/readme.md")]
        )
        self.assertTrue(freshness.needs_rereview)
        self.assertEqual(freshness.stale[0].changed_paths, ("checkout/app.py",))

    def test_unrelated_change_keeps_answer(self) -> None:
        freshness = answer_freshness(_answer(), {"shop": NEW}, [_changes("payment/app.py")])
        self.assertFalse(freshness.needs_rereview)

    def test_directory_anchor_matches_anything_below(self) -> None:
        answer = _answer(anchors=(_anchor("checkout/"),))
        touched = answer_freshness(answer, {"shop": NEW}, [_changes("/checkout/deep/x.cs")])
        self.assertTrue(touched.needs_rereview)
        sibling = answer_freshness(answer, {"shop": NEW}, [_changes("checkout-tests/x.cs")])
        self.assertFalse(sibling.needs_rereview)

    def test_repository_root_anchor_matches_everything(self) -> None:
        answer = _answer(anchors=(_anchor("/"),))
        self.assertTrue(answer_freshness(answer, {"shop": NEW}, [_changes("README.md")]).needs_rereview)

    def test_unprovable_states_need_rereview(self) -> None:
        cases = {
            "missing repository": ({}, []),
            "no comparison": ({"shop": NEW}, []),
            "rewritten history": ({"shop": NEW}, [_changes(ancestor=False)]),
        }
        for name, (commits, changes) in cases.items():
            with self.subTest(name):
                self.assertTrue(answer_freshness(_answer(), commits, changes).needs_rereview)

    def test_link_without_anchors_is_always_current(self) -> None:
        link = _answer(
            subject=ReviewSubject(kind=ReviewSubjectKind.IDENTITY, key=identity_key("a")),
            verdict=ReviewVerdict.LINK,
            linked_runtime_name="b",
            anchors=(),
        )
        self.assertFalse(answer_freshness(link, {}, []).needs_rereview)


class ReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "reviews.sqlite3"
        self.store = ReviewStore(self.path)
        self.scope = BaselineScope(tenant="acme", project_id="shop")

    def test_round_trip_history_and_scoping(self) -> None:
        later = _answer(
            answer_id="a2",
            verdict=ReviewVerdict.DISAGREE, evidence_seen=("method:STATIC_CODE",),
            reason="gone",
            answered_at=T0 + timedelta(minutes=5),
        )
        question = _answer(
            answer_id="a3", subject=QUESTION, verdict=ReviewVerdict.NOTHING_HIDDEN, reason="none"
        )
        for answer in (later, _answer(), question):
            self.store.append(self.scope, answer)
        self.store.append(BaselineScope(tenant="acme", project_id="other"), _answer(answer_id="x"))

        self.assertEqual(self.store.answers(self.scope), [_answer(), question, later])
        self.assertEqual(self.store.answers(self.scope, EDGE), [_answer(), later])
        self.assertEqual(self.store.answers(BaselineScope(tenant="acme", project_id="none")), [])

    def test_reading_never_creates_the_file(self) -> None:
        self.assertEqual(self.store.answers(self.scope), [])
        self.assertFalse(self.path.exists())

    def test_duplicate_answer_id_rejected(self) -> None:
        self.store.append(self.scope, _answer())
        with self.assertRaises(DuplicateAnswerError):
            self.store.append(self.scope, _answer(verdict=ReviewVerdict.DISAGREE, evidence_seen=("method:STATIC_CODE",), reason="x"))

    def test_corrupt_row_is_reported(self) -> None:
        self.store.append(self.scope, _answer())
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE review_answers SET record = '{}'")
        with self.assertRaises(ReviewStoreCorruptError):
            self.store.answers(self.scope)

    def test_row_must_match_its_record(self) -> None:
        self.store.append(self.scope, _answer())
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE review_answers SET subject_key = 'edge:x->y'")
        with self.assertRaisesRegex(ReviewStoreCorruptError, "does not match"):
            self.store.answers(self.scope, ReviewSubject(kind=EDGE.kind, key="edge:x->y"))

    def test_unknown_schema_version_refused(self) -> None:
        self.store.append(self.scope, _answer())
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(ReviewStoreCorruptError, "schema version"):
            self.store.answers(self.scope)

    def test_uses_rollback_journal(self) -> None:
        self.store.append(self.scope, _answer())
        with sqlite3.connect(self.path) as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "delete")


if __name__ == "__main__":
    unittest.main()

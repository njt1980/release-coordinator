"""Tests for release decision records (T3a): Part-11-ready shape, append-only hash chain."""

import base64
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import api, decision_api, webhooks
from src.core import config
from src.core.decision import (
    GENESIS_HASH,
    DecisionDraft,
    DecisionRecord,
    DecisionVerdict,
    Signer,
    SignerSource,
    assessment_digest,
    seal,
)
from src.core.models import AssessmentEvidence, ReasonCode, ReleaseAssessment
from src.tools.baseline_store import BaselineScope
from src.tools.decision_store import DecisionStore, UnknownCorrectionError
from src.tools.principal import parse_principal
from src.webhooks import PrAssessmentJob

SHA = "a" * 40
SIGNED_AT = datetime(2026, 11, 14, 10, 22, 1, tzinfo=timezone.utc)
ENTRA = Signer(name="Jane Doe", identifier="jane@contoso.com", object_id="oid-1", source=SignerSource.ENTRA)
SCOPE = BaselineScope(tenant="acme", project_id="shop")


def _draft(**changes: Any) -> DecisionDraft:
    values: dict[str, Any] = dict(
        record_id="1" * 32, tenant="acme", project_id="shop", job_id="job-1", pr_id="PR-7",
        head_sha=SHA, assessment_sha256="b" * 64, tool_approved=True, tool_actionable=True,
        verdict=DecisionVerdict.APPROVE, signer=ENTRA, signed_at=SIGNED_AT,
    )
    values.update(changes)
    return DecisionDraft(**values)


def _id(number: int) -> str:
    return f"{number:032x}"


class DecisionModelTests(unittest.TestCase):
    def test_reasons_are_required_where_a_person_departs_from_the_tool(self) -> None:
        _draft()  # approving what the tool approved needs no reason
        cases: list[dict[str, Any]] = [
            {"verdict": DecisionVerdict.DECLINE},
            {"verdict": DecisionVerdict.FURTHER_ANALYSIS},
            {"tool_approved": False},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValidationError, "reason is required"):
                _draft(**changes)
        _draft(tool_approved=False, reason="Reviewed the unmapped files by hand.")

    def test_signing_time_must_be_zoned(self) -> None:
        with self.assertRaises(ValidationError):
            _draft(signed_at=datetime(2026, 11, 14, 10, 22))

    def test_manifestation_names_meaning_signer_and_utc_time(self) -> None:
        record = seal(_draft(), 1, GENESIS_HASH)
        self.assertEqual(
            record.manifestation,
            "Approved for release: Jane Doe <jane@contoso.com>, 2026-11-14 10:22:01 UTC",
        )
        typed = seal(_draft(signer=Signer(name="Sam", identifier="Sam", source=SignerSource.TYPED)), 1, GENESIS_HASH)
        self.assertEqual(
            typed.manifestation,
            "Approved for release: Sam (typed name, not an electronic signature), 2026-11-14 10:22:01 UTC",
        )

    def test_a_changed_record_no_longer_validates(self) -> None:
        record = seal(_draft(), 1, GENESIS_HASH)
        data = record.model_dump(mode="json")
        data["verdict"] = "decline"
        data["reason"] = "x"
        with self.assertRaisesRegex(ValidationError, "record_hash does not match"):
            DecisionRecord.model_validate(data)

    def test_assessment_digest_is_key_order_independent(self) -> None:
        self.assertEqual(assessment_digest({"a": 1, "b": [2]}), assessment_digest({"b": [2], "a": 1}))
        self.assertNotEqual(assessment_digest({"a": 1}), assessment_digest({"a": 2}))


class ArchiveRoundTripTests(unittest.TestCase):
    def test_digest_survives_the_assessment_archive(self) -> None:
        from tests.test_briefing import pr_b_assessment

        assessment = pr_b_assessment()
        job = PrAssessmentJob(job_id="j", pr_id="p", status="completed", assessment=assessment)
        restored = PrAssessmentJob.model_validate_json(job.model_dump_json()).assessment
        assert restored is not None
        self.assertEqual(
            assessment_digest(assessment.model_dump(mode="json")),
            assessment_digest(restored.model_dump(mode="json")),
        )


class DecisionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "decisions" / "decisions.sqlite3"
        self.store = DecisionStore(self.path, vfs="")

    def test_reading_never_creates_the_file(self) -> None:
        self.assertEqual(self.store.records(SCOPE), [])
        self.assertTrue(self.store.verify().intact)
        self.assertFalse(self.path.exists())

    def test_records_form_one_chain(self) -> None:
        first = self.store.append(_draft(record_id=_id(1)))
        second = self.store.append(_draft(record_id=_id(2), job_id="job-2"))
        self.assertEqual((first.sequence, first.previous_hash), (1, GENESIS_HASH))
        self.assertEqual((second.sequence, second.previous_hash), (2, first.record_hash))
        self.assertEqual([r.record_id for r in self.store.records(SCOPE, "job-2")], [_id(2)])
        report = self.store.verify()
        self.assertEqual((report.records, report.last_hash, report.intact), (2, second.record_hash, True))

    def test_the_table_refuses_update_and_delete(self) -> None:
        self.store.append(_draft())
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        for statement in [
            "UPDATE decision_records SET record = '{}'",
            "DELETE FROM decision_records",
        ]:
            with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                connection.execute(statement)

    def test_tampering_outside_the_application_is_detected(self) -> None:
        self.store.append(_draft(record_id=_id(1)))
        middle = self.store.append(_draft(record_id=_id(2)))
        self.store.append(_draft(record_id=_id(3)))
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        connection.execute("DROP TRIGGER decision_records_no_delete")
        connection.execute("DELETE FROM decision_records WHERE record_id = ?", (middle.record_id,))
        connection.commit()
        problems = self.store.verify().problems
        self.assertIn("position 2: sequence 3 (a record is missing)", problems)
        self.assertIn(f"record {_id(3)}: does not link to the record before it", problems)

    def test_an_edited_record_is_detected(self) -> None:
        record = self.store.append(_draft())
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        connection.execute("DROP TRIGGER decision_records_no_update")
        text = record.model_dump_json().replace('"approve"', '"decline"')
        connection.execute("UPDATE decision_records SET record = ?", (text,))
        connection.commit()
        self.assertFalse(self.store.verify().intact)

    def test_corrections_must_name_a_decision_on_the_same_assessment(self) -> None:
        original = self.store.append(_draft(record_id=_id(1)))
        correction = self.store.append(
            _draft(record_id=_id(2), verdict=DecisionVerdict.DECLINE, reason="Wrong PR.", corrects=original.record_id)
        )
        self.assertEqual(correction.corrects, original.record_id)
        with self.assertRaises(UnknownCorrectionError):
            self.store.append(_draft(record_id=_id(3), job_id="job-2", corrects=original.record_id))
        with self.assertRaises(UnknownCorrectionError):
            self.store.append(_draft(record_id=_id(4), corrects=_id(99)))
        self.assertEqual(self.store.verify().records, 2)

    def test_concurrent_writers_keep_the_chain_intact(self) -> None:
        def write(number: int) -> None:
            DecisionStore(self.path, vfs="").append(_draft(record_id=_id(number)))

        threads = [threading.Thread(target=write, args=(number,)) for number in range(1, 9)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        report = self.store.verify()
        self.assertEqual((report.records, report.intact), (8, True))


def _claims(**payload: Any) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


class PrincipalTests(unittest.TestCase):
    def test_display_name_and_roles_come_from_the_claims(self) -> None:
        encoded = _claims(
            role_typ="http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
            claims=[
                {"typ": "name", "val": "Jane Doe"},
                {"typ": "http://schemas.microsoft.com/ws/2008/06/identity/claims/role", "val": "Release.Coordinator"},
            ],
        )
        principal = parse_principal("jane@contoso.com", "oid-1", encoded)
        assert principal is not None
        self.assertEqual(
            (principal.display_name, principal.user_name, principal.object_id, principal.roles),
            ("Jane Doe", "jane@contoso.com", "oid-1", frozenset({"Release.Coordinator"})),
        )

    def test_unreadable_claims_fall_back_to_the_user_name(self) -> None:
        principal = parse_principal("jane@contoso.com", None, "not base64 json!")
        assert principal is not None
        self.assertEqual((principal.display_name, principal.roles), ("jane@contoso.com", frozenset()))
        self.assertIsNone(parse_principal(" ", None, None))


def _assessment(head_sha: str = SHA) -> ReleaseAssessment:
    return ReleaseAssessment(
        approved=False,
        actionable=False,
        reason_codes=[ReasonCode.DISCOVERY_GAP],
        deployment_sequence=["urn:release-coord:acme:prod:aks-1:shop:orders"],
        justification="Evidence incomplete.",
        evidence=AssessmentEvidence(pr_id="PR-7", head_sha=head_sha, baseline_snapshot_id="snap-1", policy_version=3),
    )


class DecisionApiTests(unittest.TestCase):
    URL = "/api/v1/webhooks/pr-assessment/job-1/decisions"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = DecisionStore(Path(directory.name) / "d.sqlite3", vfs="")
        jobs = {
            "job-1": PrAssessmentJob(
                job_id="job-1", pr_id="PR-7", status="completed", tenant="acme", project_id="shop",
                assessment=_assessment(),
            ),
            "job-2": PrAssessmentJob(job_id="job-2", pr_id="PR-8", status="running"),
            "job-3": PrAssessmentJob(job_id="job-3", pr_id="PR-9", status="completed", assessment=_assessment("")),
        }
        for patcher in (
            patch.object(decision_api, "DECISION_STORE", store),
            patch.dict(webhooks._assessment_jobs, jobs, clear=True),
            patch.object(webhooks, "ASSESSMENT_ARCHIVE", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app)

    def test_local_decision_records_a_typed_name_and_the_assessment(self) -> None:
        response = self.client.post(
            self.URL, json={"verdict": "approve", "reason": "Unmapped files are docs.", "signer_name": "Sam"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        (view,) = response.json()
        record = view["record"]
        self.assertEqual(record["signer"], {"name": "Sam", "identifier": "Sam", "object_id": None, "source": "typed"})
        self.assertEqual((record["pr_id"], record["head_sha"], record["baseline_snapshot_id"]), ("PR-7", SHA, "snap-1"))
        self.assertEqual(record["assessment_sha256"], assessment_digest(_assessment().model_dump(mode="json")))
        self.assertFalse(record["tool_approved"])
        self.assertTrue(view["assessment_unchanged"])
        self.assertIn("not an electronic signature", view["manifestation"])

    def test_overriding_the_tool_needs_a_reason(self) -> None:
        response = self.client.post(self.URL, json={"verdict": "approve", "signer_name": "Sam"})
        self.assertEqual(response.status_code, 422)

    def test_later_decisions_supersede_earlier_ones(self) -> None:
        first = self.client.post(self.URL, json={"verdict": "further_analysis", "reason": "Check queue.", "signer_name": "Sam"})
        first_id = first.json()[0]["record"]["record_id"]
        views = self.client.post(
            self.URL,
            json={"verdict": "decline", "reason": "Queue consumer missing.", "signer_name": "Sam", "corrects": first_id},
        ).json()
        self.assertEqual([v["superseded"] for v in views], [True, False])
        self.assertEqual(views[1]["record"]["corrects"], first_id)
        self.assertEqual(len(self.client.get(self.URL).json()), 2)
        self.assertEqual(self.client.get("/api/v1/decisions/verify").json(), {
            "records": 2, "last_hash": views[1]["record"]["record_hash"], "problems": [], "intact": True,
        })

    def test_unfinished_or_commitless_assessments_cannot_be_decided(self) -> None:
        body = {"verdict": "further_analysis", "reason": "x", "signer_name": "Sam"}
        self.assertEqual(self.client.post("/api/v1/webhooks/pr-assessment/job-2/decisions", json=body).status_code, 409)
        self.assertEqual(self.client.post("/api/v1/webhooks/pr-assessment/job-3/decisions", json=body).status_code, 409)
        self.assertEqual(self.client.post("/api/v1/webhooks/pr-assessment/nope/decisions", json=body).status_code, 404)

    def test_behind_sign_in_the_signer_is_the_entra_user(self) -> None:
        body = {"verdict": "further_analysis", "reason": "x", "signer_name": "Someone Else"}
        claims = _claims(claims=[{"typ": "name", "val": "Jane Doe"}, {"typ": "roles", "val": "Release.Coordinator"}])
        headers = {"X-MS-CLIENT-PRINCIPAL-NAME": "jane@contoso.com", "X-MS-CLIENT-PRINCIPAL-ID": "oid-1", "X-MS-CLIENT-PRINCIPAL": claims}
        with patch.object(config, "TRUST_PRINCIPAL_HEADER", True), patch.object(config, "DECISION_ROLE", "Release.Coordinator"):
            self.assertEqual(self.client.post(self.URL, json=body).status_code, 401)
            no_role = {**headers, "X-MS-CLIENT-PRINCIPAL": _claims(claims=[])}
            self.assertEqual(self.client.post(self.URL, json=body, headers=no_role).status_code, 403)
            response = self.client.post(self.URL, json=body, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        with patch.object(config, "TRUST_PRINCIPAL_HEADER", True), patch.object(config, "DECISION_ROLE", "Release.Coordinator"):
            me = self.client.get("/api/v1/decisions/signer", headers=headers).json()
            anonymous = self.client.get("/api/v1/decisions/signer").json()
        self.assertEqual((me["source"], me["name"], me["can_decide"]), ("entra", "Jane Doe", True))
        self.assertFalse(anonymous["can_decide"])
        self.assertEqual(self.client.get("/api/v1/decisions/signer").json()["source"], "typed")
        signer = response.json()[0]["record"]["signer"]
        self.assertEqual(signer, {"name": "Jane Doe", "identifier": "jane@contoso.com", "object_id": "oid-1", "source": "entra"})


if __name__ == "__main__":
    unittest.main()

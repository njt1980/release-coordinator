"""Tests for the read-only assistant: its tools, conversation loop and API."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import anthropic
import httpx2
from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock
from fastapi import HTTPException

from src import assistant_api
from src.assistant.chat import MAX_TOOL_ROUNDS, Assistant, AssistantUnavailableError, ChatContext
from src.assistant.tools import AssistantTools
from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryStageReport,
    DiscoveryStatus,
    KnowledgeGraph,
    RunEventStatus,
    ScanContext,
    ServiceNode,
)
from src.orchestration.run_log import EventDraft, RunLogStore
from src.tools.baseline_store import BaselineScope, BaselineStore
from src.tools.policy_store import PolicyStore

URN = "urn:release-coord:acme:prod:aks-1:shop"
SCOPE = BaselineScope(tenant="acme", project_id="shop")
POLICY = """\
policy_version: 4
tenant: acme
project_id: shop
environment: prod
cluster: aks-1
required_sources: [STATIC_CODE]
runtime_evidence: baseline
"""


def _edge(source: str, target: str, method: DiscoveryMethod) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[method],
        confidence_score=1.0,
        last_observed_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
    )


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.baselines = BaselineStore(root / "baselines")
        self.baselines.write(
            SCOPE,
            KnowledgeGraph(
                services=[
                    ServiceNode(service_id=f"{URN}:order", name="order"),
                    ServiceNode(service_id=f"{URN}:db", name="db"),
                ],
                edges=[_edge(f"{URN}:order", f"{URN}:db", DiscoveryMethod.RUNTIME_TELEMETRY)],
                discovery=[DiscoveryStageReport(source=m, status=DiscoveryStatus.SUCCESS) for m in DiscoveryMethod],
                scan=ScanContext(environment="prod", cluster="aks-1", namespaces=["shop"]),
            ),
        )
        policies = root / "policies"
        (policies / "acme").mkdir(parents=True)
        (policies / "acme" / "shop.yaml").write_text(POLICY, encoding="utf-8")
        self.runs = RunLogStore()
        self.runs.start("run-1", "assessment", "Assess PR-1", "acme", "shop")
        self.runs.add("run-1", EventDraft(stage="Verdict", status=RunEventStatus.BLOCKED, message="Not approved: DISCOVERY_GAP."))
        self.assessments = {
            "run-1": MagicMock(tenant="acme", project_id="shop", model_dump=lambda **_: {"status": "completed"})
        }
        self.tools = AssistantTools(
            baselines=self.baselines,
            policies=PolicyStore(policies),
            runs=self.runs,
            find_assessment=self.assessments.get,
        )

    def _run(self, name: str, arguments: dict[str, Any]) -> tuple[Any, bool]:
        text, is_error = self.tools.run(name, arguments)
        return (text if is_error else json.loads(text)), is_error

    def test_definitions_describe_every_tool(self) -> None:
        names = [d["name"] for d in self.tools.definitions()]
        self.assertEqual(
            names,
            ["get_baseline_overview", "get_service", "get_policy", "list_runs", "get_run_log", "get_assessment", "explain_reason_code"],
        )
        for definition in self.tools.definitions():
            schema = definition["input_schema"]
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(sorted(schema["required"]), sorted(schema["properties"]))

    def test_baseline_and_service_lookups(self) -> None:
        overview, error = self._run("get_baseline_overview", {"tenant": "acme", "project_id": "shop"})
        self.assertFalse(error)
        self.assertEqual(overview["services"], ["shop/db", "shop/order"])
        service, _ = self._run("get_service", {"tenant": "acme", "project_id": "shop", "service": "order"})
        self.assertEqual(service["calls"][0]["service"], "shop/db")
        self.assertEqual(service["calls"][0]["found_by"], ["RUNTIME_TELEMETRY"])
        missing, error = self._run("get_service", {"tenant": "acme", "project_id": "shop", "service": "nope"})
        self.assertTrue(error)
        self.assertIn("known services", missing)

    def test_policy_runs_assessment_and_reasons(self) -> None:
        policy, _ = self._run("get_policy", {"tenant": "acme", "project_id": "shop"})
        self.assertEqual((policy["status"], policy["policy"]["cluster"]), ("valid", "aks-1"))
        absent, _ = self._run("get_policy", {"tenant": "acme", "project_id": "other"})
        self.assertEqual(absent["status"], "PolicyMissingError")
        runs, _ = self._run("list_runs", {})
        self.assertEqual(runs[0]["run_id"], "run-1")
        log, _ = self._run("get_run_log", {"run_id": "run-1"})
        self.assertEqual(log["events"][0]["message"], "Not approved: DISCOVERY_GAP.")
        verdict, _ = self._run("get_assessment", {"run_id": "run-1"})
        self.assertEqual(verdict, {"status": "completed"})
        reason, _ = self._run("explain_reason_code", {"code": "baseline_drift"})
        self.assertIn("Re-run onboarding", reason["usual_fix"])

    def test_bad_input_is_an_error_result_not_an_exception(self) -> None:
        for name, arguments in [
            ("get_policy", {"tenant": "../etc", "project_id": "shop"}),
            ("get_policy", {"tenant": "acme", "project_id": "shop", "extra": 1}),
            ("get_run_log", {"run_id": "missing"}),
            ("explain_reason_code", {"code": "NOPE"}),
            ("delete_everything", {}),
        ]:
            with self.subTest(name=name, arguments=arguments):
                _, error = self._run(name, arguments)
                self.assertTrue(error)


def _text(text: str) -> BetaTextBlock:
    return BetaTextBlock(type="text", text=text)


def _tool_use(name: str, arguments: dict[str, Any], id_: str = "tu_1") -> BetaToolUseBlock:
    return BetaToolUseBlock(type="tool_use", id=id_, name=name, input=arguments)


def _response(stop: str, *blocks: Any) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop, content=list(blocks), model="claude-opus-5-5")


class ChatLoopTests(unittest.TestCase):
    def _assistant(self, *responses: SimpleNamespace) -> tuple[Assistant, MagicMock, MagicMock]:
        client = MagicMock()
        client.beta.messages.create.side_effect = list(responses)
        tools = MagicMock()
        tools.definitions.return_value = [{"name": "list_runs"}]
        tools.run.return_value = ('[{"run_id": "r"}]', False)
        return Assistant(tools=tools, client=client), client, tools

    def test_tool_results_go_back_and_the_turn_is_remembered(self) -> None:
        assistant, client, tools = self._assistant(
            _response("tool_use", _text("Let me look."), _tool_use("list_runs", {})),
            _response("end_turn", _text("Run r was blocked.")),
            _response("end_turn", _text("Because of DISCOVERY_GAP.")),
        )
        first = assistant.ask("Why was it blocked?", ChatContext(page="runs", run_id="r"))
        self.assertEqual(first.reply, "Run r was blocked.")
        self.assertEqual([(t.name, t.is_error) for t in first.tools_used], [("list_runs", False)])
        tools.run.assert_called_once_with("list_runs", {})

        second_call = client.beta.messages.create.call_args_list[1].kwargs
        self.assertEqual(second_call["model"], "claude-opus-5-5")
        self.assertEqual(second_call["fallbacks"], "default")
        self.assertEqual(second_call["thinking"], {"type": "adaptive"})
        messages = second_call["messages"]
        self.assertIn("looking at run r", messages[0]["content"][0]["text"])
        self.assertEqual(messages[2]["content"][0]["type"], "tool_result")
        self.assertEqual(messages[2]["content"][0]["tool_use_id"], "tu_1")

        second = assistant.ask("Why?", ChatContext(), first.conversation_id)
        self.assertEqual(second.conversation_id, first.conversation_id)
        # The mock keeps a reference to the list the loop goes on appending
        # to, so check the order rather than the length.
        history = client.beta.messages.create.call_args_list[2].kwargs["messages"]
        self.assertEqual([m["role"] for m in history[:5]], ["user", "assistant", "user", "assistant", "user"])
        self.assertIn("Why?", history[4]["content"][0]["text"])

    def test_runaway_tool_use_is_cut_off(self) -> None:
        looping = [_response("tool_use", _tool_use("list_runs", {}, f"tu_{i}")) for i in range(MAX_TOOL_ROUNDS + 1)]
        assistant, client, _ = self._assistant(*looping)
        turn = assistant.ask("loop", ChatContext())
        self.assertEqual(client.beta.messages.create.call_count, MAX_TOOL_ROUNDS + 1)
        self.assertIn("narrower question", turn.reply)

    def test_refusals_and_missing_credentials(self) -> None:
        assistant, _, _ = self._assistant(_response("refusal"))
        self.assertIn("declined", assistant.ask("x", ChatContext()).reply)

        client = MagicMock()
        client.beta.messages.create.side_effect = TypeError(
            "Could not resolve authentication method. Expected api_key to be set."
        )
        with self.assertRaises(AssistantUnavailableError):
            Assistant(tools=MagicMock(), client=client).ask("x", ChatContext())


class ApiTests(unittest.TestCase):
    def test_errors_map_to_http_statuses(self) -> None:
        request = assistant_api.ChatRequest(message="hi")
        response = httpx2.Response(429, request=httpx2.Request("POST", "https://api.anthropic.com"))
        for error, code in [
            (AssistantUnavailableError("no key"), 503),
            (anthropic.RateLimitError("slow down", response=response, body=None), 429),
        ]:
            with self.subTest(code=code), patch.object(
                assistant_api.ASSISTANT, "ask", side_effect=error
            ):
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(assistant_api.chat(request))
                self.assertEqual(caught.exception.status_code, code)

    def test_status_reports_configuration(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x"}):
            self.assertTrue(assistant_api.assistant_status().configured)
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(assistant_api.assistant_status().configured)


if __name__ == "__main__":
    unittest.main()

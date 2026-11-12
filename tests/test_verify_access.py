"""Tests for the pre-flight read-only access verification workflow."""

import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from src import api
from src.api import (
    DevOpsVerificationRequest,
    KubernetesVerificationRequest,
    OnboardingPayload,
    SystemVerification,
    TelemetryVerificationRequest,
)
from src.core.models import (
    DiscoveryMethod,
    DiscoveryResult,
    DiscoveryStageReport,
    DiscoveryStatus,
)
from src.discovery_engine import nodes as discovery_nodes
from src.discovery_engine.state import DiscoveryGraphState
from src.tools import _timeout
from src.tools._timeout import TIMEOUT_MESSAGE
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor


@contextmanager
def _hanging_call(module: object) -> Iterator[threading.Event]:
    """Shrink the verify deadline and yield an event a fake SDK call blocks on."""
    release = threading.Event()
    with patch.object(module, "VERIFY_TIMEOUT_SECONDS", 0.05):
        try:
            yield release
        finally:
            release.set()


class RunWithTimeoutTests(unittest.TestCase):
    def test_returns_result(self) -> None:
        self.assertEqual(_timeout.run_with_timeout(lambda: 42, 1.0), 42)

    def test_reraises_errors(self) -> None:
        def boom() -> None:
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            _timeout.run_with_timeout(boom, 1.0)

    def test_raises_timeout_error(self) -> None:
        release = threading.Event()
        try:
            with self.assertRaises(TimeoutError):
                _timeout.run_with_timeout(release.wait, 0.05)
        finally:
            release.set()


class DevOpsVerifyAccessTests(unittest.TestCase):
    def _extractor(self) -> DevOpsExtractor:
        extractor = DevOpsExtractor.__new__(DevOpsExtractor)
        extractor._credential = MagicMock()
        extractor._organization_url = "https://dev.azure.com/org"
        extractor._project = "proj"
        return extractor

    def test_success_fetches_project_metadata(self) -> None:
        extractor = self._extractor()
        connection = MagicMock()
        with patch.object(extractor, "_get_connection", return_value=connection):
            self.assertEqual(extractor.verify_access(), (True, "Success"))
        connection.clients.get_core_client().get_project.assert_called_once_with(
            "proj"
        )

    def test_constructor_binds_explicit_scope(self) -> None:
        with patch.dict(
            "os.environ",
            {"AZURE_DEVOPS_ORG_URL": "https://env", "AZURE_DEVOPS_PROJECT": "env"},
        ), patch("src.tools.devops_extractor.DefaultAzureCredential"):
            extractor = DevOpsExtractor(
                org_url=" https://dev.azure.com/org ",
                project="proj",
                repositories=["repo-a"],
            )
        self.assertEqual(extractor._organization_url, "https://dev.azure.com/org")
        self.assertEqual(extractor._project, "proj")
        self.assertEqual(extractor._repositories, ["repo-a"])

    def test_constructor_rejects_blank_scope(self) -> None:
        with patch("src.tools.devops_extractor.DefaultAzureCredential"):
            with self.assertRaises(ValueError):
                DevOpsExtractor(org_url=" ", project="proj", repositories=[])
            with self.assertRaises(ValueError):
                DevOpsExtractor(
                    org_url="https://dev.azure.com/org", project="", repositories=[]
                )

    def test_connection_uses_constructor_org_url(self) -> None:
        from src.tools import devops_extractor

        extractor = self._extractor()
        extractor._credential.get_token.return_value = MagicMock(token="t")
        with patch.object(devops_extractor, "Connection") as connection_cls:
            extractor._get_connection()
        self.assertEqual(
            connection_cls.call_args.kwargs["base_url"], "https://dev.azure.com/org"
        )

    def test_hanging_token_request_times_out(self) -> None:
        from src.tools import devops_extractor

        extractor = self._extractor()
        with _hanging_call(devops_extractor) as release:
            extractor._credential.get_token.side_effect = lambda _: release.wait()
            self.assertEqual(extractor.verify_access(), (False, TIMEOUT_MESSAGE))


class KubernetesVerifyAccessTests(unittest.TestCase):
    def _extractor(self, api_client: MagicMock | None) -> KubernetesExtractor:
        extractor = KubernetesExtractor.__new__(KubernetesExtractor)
        extractor._core_v1_api = api_client
        return extractor

    def test_success_lists_one_service(self) -> None:
        api_client = MagicMock()
        extractor = self._extractor(api_client)
        self.assertEqual(extractor.verify_access("payments"), (True, "Success"))
        api_client.list_namespaced_service.assert_called_once_with(
            "payments", limit=1, _request_timeout=5.0
        )

    def test_hanging_request_times_out(self) -> None:
        from src.tools import k8s_extractor

        api_client = MagicMock()
        with _hanging_call(k8s_extractor) as release:
            api_client.list_namespaced_service.side_effect = (
                lambda *_, **__: release.wait()
            )
            self.assertEqual(
                self._extractor(api_client).verify_access("payments"),
                (False, TIMEOUT_MESSAGE),
            )

    def test_forbidden_returns_error(self) -> None:
        api_client = MagicMock()
        api_client.list_namespaced_service.side_effect = Exception("Forbidden")
        ok, message = self._extractor(api_client).verify_access("payments")
        self.assertFalse(ok)
        self.assertEqual(message, "Forbidden")

    def test_unconfigured_client_fails(self) -> None:
        ok, _ = self._extractor(None).verify_access("payments")
        self.assertFalse(ok)


class TelemetryVerifyAccessTests(unittest.TestCase):
    def _extractor(self, source: str) -> TelemetryExtractor:
        extractor = TelemetryExtractor.__new__(TelemetryExtractor)
        extractor._source = source
        extractor._client = MagicMock()
        extractor._workspace_id = "ws-1"
        return extractor

    def test_constructor_binds_explicit_workspace(self) -> None:
        with patch.dict(
            "os.environ",
            {"TELEMETRY_SOURCE": "mock", "LOG_ANALYTICS_WORKSPACE_ID": "env-ws"},
        ):
            extractor = TelemetryExtractor(workspace_id=" ws-1 ")
        self.assertEqual(extractor._workspace_id, "ws-1")

    def test_constructor_rejects_blank_workspace(self) -> None:
        with patch.dict(
            "os.environ",
            {"TELEMETRY_SOURCE": "mock", "LOG_ANALYTICS_WORKSPACE_ID": "env-ws"},
        ):
            with self.assertRaises(ValueError):
                TelemetryExtractor(workspace_id="  ")

    def test_mock_source_succeeds_without_query(self) -> None:
        extractor = self._extractor("mock")
        self.assertEqual(extractor.verify_access(), (True, "Success"))
        extractor._client.query_workspace.assert_not_called()

    def test_azure_source_runs_trivial_query(self) -> None:
        extractor = self._extractor("azure")
        extractor._client.query_workspace.return_value = MagicMock(tables=[])
        self.assertEqual(extractor.verify_access(), (True, "Success"))
        args, _ = extractor._client.query_workspace.call_args
        self.assertEqual(args[:2], ("ws-1", "print ok=1"))

    def test_azure_query_error_fails(self) -> None:
        extractor = self._extractor("azure")
        extractor._client.query_workspace.side_effect = Exception("denied")
        self.assertEqual(extractor.verify_access(), (False, "denied"))

    def test_azure_hanging_query_times_out(self) -> None:
        from src.tools import telemetry_extractor

        extractor = self._extractor("azure")
        with _hanging_call(telemetry_extractor) as release:
            extractor._client.query_workspace.side_effect = (
                lambda *_, **__: release.wait()
            )
            self.assertEqual(extractor.verify_access(), (False, TIMEOUT_MESSAGE))


class VerifyEndpointTests(unittest.TestCase):
    def test_devops_endpoint_maps_result(self) -> None:
        payload = DevOpsVerificationRequest(
            devops_org_url="https://dev.azure.com/org",
            devops_project="proj",
            target_repositories=["repo-a"],
        )
        with patch.object(api, "DevOpsExtractor") as devops_cls:
            devops_cls.return_value.verify_access.return_value = (True, "Success")
            response = api.verify_devops_endpoint(payload)
        self.assertEqual(response, SystemVerification(status=True, message="Success"))
        devops_cls.assert_called_once_with(
            org_url="https://dev.azure.com/org",
            project="proj",
            repositories=["repo-a"],
        )

    def test_devops_endpoint_rejects_missing_scope(self) -> None:
        with self.assertRaises(ValidationError):
            DevOpsVerificationRequest.model_validate({})
        with self.assertRaises(ValidationError):
            DevOpsVerificationRequest(devops_org_url="", devops_project="proj")

    def test_onboarding_state_carries_devops_scope(self) -> None:
        payload = OnboardingPayload(
            devops_org_url="https://dev.azure.com/org",
            devops_project="proj",
            target_repositories=["repo-a"],
            target_namespaces=["default"],
            workspace_id="ws-1",
        )
        state = api.build_discovery_state(payload)
        self.assertEqual(state["devops_org_url"], "https://dev.azure.com/org")
        self.assertEqual(state["devops_project"], "proj")

    def test_kubernetes_endpoint_reports_failing_namespaces(self) -> None:
        payload = KubernetesVerificationRequest(target_namespaces=["ns-a", "ns-b"])
        k8s = MagicMock()
        k8s.verify_access.side_effect = [(True, "Success"), (False, "Forbidden")]
        with patch.object(api, "KubernetesExtractor", return_value=k8s):
            response = api.verify_kubernetes_endpoint(payload)
        self.assertFalse(response.status)
        self.assertEqual(response.message, "ns-b: Forbidden")

    def test_telemetry_endpoint_reports_construction_errors(self) -> None:
        payload = TelemetryVerificationRequest(workspace_id="ws-1")
        with patch.object(api, "TelemetryExtractor") as telemetry_cls:
            telemetry_cls.side_effect = ValueError("bad TELEMETRY_SOURCE")
            response = api.verify_telemetry_endpoint(payload)
        self.assertFalse(response.status)
        self.assertEqual(response.message, "bad TELEMETRY_SOURCE")

    def test_telemetry_endpoint_binds_workspace(self) -> None:
        payload = TelemetryVerificationRequest(workspace_id=" ws-1 ")
        with patch.object(api, "TelemetryExtractor") as telemetry_cls:
            telemetry_cls.return_value.verify_access.return_value = (True, "Success")
            response = api.verify_telemetry_endpoint(payload)
        self.assertTrue(response.status)
        telemetry_cls.assert_called_once_with(workspace_id="ws-1")

    def test_telemetry_endpoint_rejects_blank_workspace(self) -> None:
        with self.assertRaises(ValidationError):
            TelemetryVerificationRequest(workspace_id="   ")

    def test_routes_are_registered(self) -> None:
        paths = {route.path for route in api.app.routes}
        self.assertTrue(
            {
                "/api/verify/devops",
                "/api/verify/kubernetes",
                "/api/verify/telemetry",
            }.issubset(paths)
        )
        self.assertNotIn("/api/verify", paths)


class OnboardingPayloadTests(unittest.TestCase):
    def test_accepts_devops_only(self) -> None:
        payload = OnboardingPayload(
            devops_org_url=" https://dev.azure.com/org ",
            devops_project="proj",
            target_repositories=["repo-a", " ", ""],
        )
        self.assertEqual(payload.devops_org_url, "https://dev.azure.com/org")
        self.assertEqual(payload.target_repositories, ["repo-a"])
        self.assertEqual(payload.target_namespaces, [])
        self.assertEqual(payload.workspace_id, "")

    def test_accepts_kubernetes_only(self) -> None:
        payload = OnboardingPayload(target_namespaces=[" default ", ""])
        self.assertEqual(payload.target_namespaces, ["default"])
        self.assertFalse(payload.has_devops)

    def test_accepts_telemetry_only(self) -> None:
        payload = OnboardingPayload.model_validate({"workspace_id": " ws-1 "})
        self.assertEqual(payload.workspace_id, "ws-1")

    def test_rejects_empty_payload(self) -> None:
        with self.assertRaises(ValidationError):
            OnboardingPayload.model_validate({})
        with self.assertRaises(ValidationError):
            OnboardingPayload(
                devops_org_url=" ",
                target_namespaces=[" "],
                workspace_id="  ",
            )

    def test_rejects_org_url_without_project(self) -> None:
        with self.assertRaises(ValidationError):
            OnboardingPayload(
                devops_org_url="https://dev.azure.com/org",
                workspace_id="ws-1",
            )

    def test_rejects_project_without_org_url(self) -> None:
        with self.assertRaises(ValidationError):
            OnboardingPayload(devops_project="proj", workspace_id="ws-1")

    def test_rejects_repositories_without_devops_scope(self) -> None:
        with self.assertRaises(ValidationError):
            OnboardingPayload(target_repositories=["repo-a"], workspace_id="ws-1")


def _discovery_state(**overrides: object) -> DiscoveryGraphState:
    state: DiscoveryGraphState = {
        "workspace_id": "",
        "devops_org_url": "",
        "devops_project": "",
        "target_repositories": [],
        "target_namespaces": [],
        "discovered_services": [],
        "static_edges": [],
        "runtime_edges": [],
        "discovery_reports": [],
        "reconciled_graph": {},
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def _skipped(source: DiscoveryMethod) -> dict[str, object]:
    return {
        "discovery_reports": [
            DiscoveryStageReport(source=source, status=DiscoveryStatus.SKIPPED)
        ]
    }


class DiscoveryNodeSkipTests(unittest.TestCase):
    def test_empty_scope_stays_empty(self) -> None:
        self.assertEqual(
            discovery_nodes.initialize_scan_scope(_discovery_state()),
            {"target_repositories": [], "target_namespaces": []},
        )

    def test_supplied_scope_is_preserved(self) -> None:
        state = _discovery_state(
            target_repositories=["repo-a"], target_namespaces=["prod"]
        )
        self.assertEqual(
            discovery_nodes.initialize_scan_scope(state),
            {"target_repositories": ["repo-a"], "target_namespaces": ["prod"]},
        )

    def test_static_scan_skipped_without_devops_scope(self) -> None:
        with patch.object(discovery_nodes, "DevOpsExtractor") as devops_cls:
            result = discovery_nodes.extract_static_code_baseline(
                _discovery_state(devops_org_url="https://dev.azure.com/org")
            )
        self.assertEqual(result, _skipped(DiscoveryMethod.STATIC_CODE))
        devops_cls.assert_not_called()

    def test_static_scan_runs_with_devops_scope(self) -> None:
        with patch.object(discovery_nodes, "DevOpsExtractor") as devops_cls:
            devops_cls.return_value.scan_repository_for_endpoints.return_value = (
                DiscoveryResult.success([])
            )
            discovery_nodes.extract_static_code_baseline(
                _discovery_state(
                    devops_org_url="https://dev.azure.com/org",
                    devops_project="proj",
                    target_repositories=["repo-a"],
                )
            )
        devops_cls.assert_called_once_with(
            org_url="https://dev.azure.com/org",
            project="proj",
            repositories=["repo-a"],
        )

    def test_k8s_skipped_without_namespaces(self) -> None:
        with patch.object(discovery_nodes, "KubernetesExtractor") as k8s_cls:
            result = discovery_nodes.extract_k8s_topology(_discovery_state())
        self.assertEqual(result, _skipped(DiscoveryMethod.K8S_MANIFEST))
        k8s_cls.assert_not_called()

    def test_telemetry_skipped_without_workspace(self) -> None:
        with patch.object(discovery_nodes, "TelemetryExtractor") as telemetry_cls:
            result = discovery_nodes.extract_runtime_telemetry(
                _discovery_state(workspace_id="  ")
            )
        self.assertEqual(result, _skipped(DiscoveryMethod.RUNTIME_TELEMETRY))
        telemetry_cls.assert_not_called()

    def test_telemetry_binds_workspace(self) -> None:
        with patch.object(discovery_nodes, "TelemetryExtractor") as telemetry_cls:
            telemetry_cls.return_value.get_active_dependencies.return_value = (
                DiscoveryResult.success([])
            )
            result = discovery_nodes.extract_runtime_telemetry(
                _discovery_state(workspace_id="ws-1")
            )
        self.assertEqual(
            result,
            {
                "runtime_edges": [],
                "discovery_reports": [
                    DiscoveryStageReport(
                        source=DiscoveryMethod.RUNTIME_TELEMETRY,
                        status=DiscoveryStatus.SUCCESS,
                    )
                ],
            },
        )
        telemetry_cls.assert_called_once_with(workspace_id="ws-1")


if __name__ == "__main__":
    unittest.main()

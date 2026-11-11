"""Tests for the pre-flight read-only access verification workflow."""

import unittest
from unittest.mock import MagicMock, patch

from src import api
from src.api import OnboardingPayload, VerificationResponse
from src.tools.devops_extractor import DevOpsExtractor
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.telemetry_extractor import TelemetryExtractor


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

    def test_missing_configuration_fails(self) -> None:
        extractor = self._extractor()
        extractor._project = None
        ok, message = extractor.verify_access()
        self.assertFalse(ok)
        self.assertIn("AZURE_DEVOPS_PROJECT", message)


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
            "payments", limit=1
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
        extractor._resource_id = None
        extractor._workspace_id = "ws-1"
        return extractor

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


class VerifyEndpointTests(unittest.TestCase):
    def test_maps_each_system_result(self) -> None:
        payload = OnboardingPayload(
            target_repositories=["repo"],
            target_namespaces=["ns-a", "ns-b"],
            workspace_id="ws",
        )
        k8s = MagicMock()
        k8s.verify_access.side_effect = [(True, "Success"), (False, "Forbidden")]
        with (
            patch.object(api, "DevOpsExtractor") as devops_cls,
            patch.object(api, "KubernetesExtractor", return_value=k8s),
            patch.object(api, "TelemetryExtractor") as telemetry_cls,
        ):
            devops_cls.return_value.verify_access.return_value = (True, "Success")
            telemetry_cls.side_effect = ValueError("bad TELEMETRY_SOURCE")
            response = api.verify(payload)

        self.assertIsInstance(response, VerificationResponse)
        self.assertTrue(response.devops.status)
        self.assertFalse(response.kubernetes.status)
        self.assertEqual(response.kubernetes.message, "ns-b: Forbidden")
        self.assertFalse(response.telemetry.status)
        self.assertEqual(response.telemetry.message, "bad TELEMETRY_SOURCE")


if __name__ == "__main__":
    unittest.main()

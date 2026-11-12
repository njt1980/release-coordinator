import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.models import ServiceNode
from src.discovery_engine import nodes as discovery_nodes
from src.discovery_engine.state import DiscoveryGraphState
from src.tools import devops_extractor
from src.tools.devops_extractor import DevOpsExtractor


class ScannablePathTests(unittest.TestCase):
    def test_application_and_config_files_are_scanned(self) -> None:
        for path in [
            "/src/app.py",
            "/web/client.ts",
            "/deploy/values.yaml",
            "/appsettings.json",
            "/Api/Program.cs",
            "/.env",
            "/config/.env.production",
        ]:
            with self.subTest(path=path):
                self.assertTrue(devops_extractor._is_scannable_path(path))

    def test_markdown_text_and_docs_are_skipped(self) -> None:
        for path in [
            "/README.md",
            "/notes.txt",
            "/docs/setup.yaml",
            "/service/Docs/config.json",
            "/LICENSE",
        ]:
            with self.subTest(path=path):
                self.assertFalse(devops_extractor._is_scannable_path(path))


class NoiseEndpointTests(unittest.TestCase):
    def test_noise_urls_are_dropped(self) -> None:
        for url in [
            "http://localhost:8080/api",
            "http://127.0.0.1:5000",
            "http://${storefrontip",
            "https://schemas.xmlsoap.org/soap",
            "https://opensource.microsoft.com/codeofconduct",
            "https://www.mongodb.org/static/pgp",
            "http://www.w3.org/2001/XMLSchema",
            "https://json.schemastore.org/package",
        ]:
            with self.subTest(url=url):
                self.assertTrue(devops_extractor._is_noise_endpoint(url))

    def test_service_urls_are_kept(self) -> None:
        for url in [
            "https://payments.internal.example.com/v1",
            "http://inventory-service:8080",
        ]:
            with self.subTest(url=url):
                self.assertFalse(devops_extractor._is_noise_endpoint(url))


def _extractor_with_files(files: dict[str, str]) -> DevOpsExtractor:
    with patch.object(devops_extractor, "DefaultAzureCredential"):
        extractor = DevOpsExtractor(
            org_url="https://dev.azure.com/org", project="proj", repositories=[]
        )
    git_client = MagicMock()
    git_client.get_repositories.return_value = [
        SimpleNamespace(name="repo-a", id="id-a")
    ]
    git_client.get_items.return_value = [
        SimpleNamespace(path=path, is_folder=False) for path in files
    ]
    git_client.get_item_content.side_effect = (
        lambda _repo_id, path, project: files[path].encode()
    )
    extractor._get_clients = MagicMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), git_client, "proj")
    )
    return extractor


class ScanRepositoryTests(unittest.TestCase):
    def test_scan_skips_noise_files_and_urls(self) -> None:
        extractor = _extractor_with_files(
            {
                "/README.md": "https://orders.example.com",
                "/docs/api.yaml": "https://docs-only.example.com",
                "/config.yaml": (
                    "url: https://payments.example.com/v1\n"
                    "local: http://localhost:3000\n"
                    "schema: https://json.schemastore.org/x\n"
                ),
            }
        )
        self.assertEqual(
            extractor.scan_repository_for_endpoints("repo-a"),
            ["https://payments.example.com/v1"],
        )

    def test_missing_repository_returns_none(self) -> None:
        extractor = _extractor_with_files({})
        self.assertIsNone(extractor.scan_repository_for_endpoints("missing"))


def _devops_state() -> DiscoveryGraphState:
    return {
        "workspace_id": "",
        "devops_org_url": "https://dev.azure.com/org",
        "devops_project": "proj",
        "target_repositories": ["repo-a", "repo-b"],
        "target_namespaces": [],
        "discovered_services": [],
        "static_edges": [],
        "runtime_edges": [],
        "reconciled_graph": {},
    }


class StaticBaselineServiceRegistrationTests(unittest.TestCase):
    def test_scanned_repositories_are_registered_as_services(self) -> None:
        with patch.object(discovery_nodes, "DevOpsExtractor") as devops_cls:
            devops_cls.return_value.scan_repository_for_endpoints.side_effect = [
                ["https://payments.example.com/v1"],
                None,
            ]
            result = discovery_nodes.extract_static_code_baseline(_devops_state())

        services = result["discovered_services"]
        assert isinstance(services, list)
        self.assertEqual(
            services,
            [
                ServiceNode(
                    service_id="repo-a",
                    name="repo-a",
                    metadata={"source": "STATIC_CODE", "devops_project": "proj"},
                )
            ],
        )
        edges = result["static_edges"]
        assert isinstance(edges, list)
        self.assertEqual(
            [(e.source_service_id, e.target_service_id) for e in edges],
            [("repo-a", "payments.example.com")],
        )


if __name__ == "__main__":
    unittest.main()

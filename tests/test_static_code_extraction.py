import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.models import ServiceNode
from src.discovery_engine import nodes as discovery_nodes
from src.discovery_engine.state import DiscoveryGraphState
from src.tools import devops_extractor
from src.tools.devops_extractor import DevOpsExtractor, EndpointReference


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
            "/src/store-front/node_modules/vite/index.js",
            "/src/store-front/package-lock.json",
            "/vendor/lib/client.go",
        ]:
            with self.subTest(path=path):
                self.assertFalse(devops_extractor._is_scannable_path(path))


class ServiceNameFromPathTests(unittest.TestCase):
    def test_files_under_a_service_directory_map_to_that_service(self) -> None:
        for path, expected in [
            ("/src/order-service/routes/root.js", "order-service"),
            ("/services/Payments/app.py", "payments"),
            ("/src/makeline-service/main.go", "makeline-service"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(
                    devops_extractor._service_name_from_path(path), expected
                )

    def test_files_outside_service_directories_have_no_service(self) -> None:
        for path in ["/config.yaml", "/charts/values.yaml", "/src/app.py"]:
            with self.subTest(path=path):
                self.assertIsNone(devops_extractor._service_name_from_path(path))


class NoiseEndpointTests(unittest.TestCase):
    def test_noise_urls_are_dropped(self) -> None:
        for url in [
            "http://localhost:8080/api",
            "http://127.0.0.1:5000",
            "http://x.x.x.x:3000",
            "http://10.0.0.12:8080",
            "http://${storefrontip",
            "https://schemas.xmlsoap.org/soap",
            "https://github.com/Azure-Samples/aks-store-demo",
            "https://paypal.me/someone",
            "https://eslint.org/docs/rules",
            "https://payments.internal.example.com/v1",
            "https://kubernetes.default.svc/api",
            "ftp://order-service/file",
        ]:
            with self.subTest(url=url):
                self.assertTrue(devops_extractor._is_noise_endpoint(url))

    def test_internal_service_urls_are_kept(self) -> None:
        for url in [
            "http://inventory-service:8080",
            "http://order-service:3000/order",
            "http://product-service.default.svc.cluster.local:3002",
            "http://makeline-service.default.svc",
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
    def test_scan_attributes_internal_endpoints_to_owning_service(self) -> None:
        extractor = _extractor_with_files(
            {
                "/README.md": "http://order-service:3000",
                "/docs/api.yaml": "http://order-service:3000",
                "/config.yaml": "url: http://order-service:3000\n",
                "/src/store-front/src/api.js": (
                    "const a = 'http://order-service:3000/order'\n"
                    "const b = 'http://product-service:3002/'\n"
                    "const c = 'https://github.com/x'\n"
                ),
                "/src/store-front/node_modules/x/index.js": "http://evil:1/",
                "/src/order-service/app.js": "http://x.x.x.x:5672",
            }
        )

        references = extractor.scan_repository_for_endpoints("repo-a")

        assert references is not None
        self.assertEqual(
            [(r.service_name, r.endpoint) for r in references],
            [
                ("store-front", "http://order-service:3000/order"),
                ("store-front", "http://product-service:3002/"),
            ],
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


def _reference(service: str, endpoint: str) -> EndpointReference:
    return EndpointReference(
        service_name=service, endpoint=endpoint, file_path=f"/src/{service}/a.js"
    )


class StaticBaselineServiceRegistrationTests(unittest.TestCase):
    def test_edges_originate_from_microservices_not_repositories(self) -> None:
        with patch.object(discovery_nodes, "DevOpsExtractor") as devops_cls:
            devops_cls.return_value.scan_repository_for_endpoints.side_effect = [
                [
                    _reference("store-front", "http://order-service:3000/order"),
                    _reference("store-front", "http://product-service:3002/"),
                    _reference("order-service", "http://rabbitmq:5672"),
                ],
                None,
            ]
            result = discovery_nodes.extract_static_code_baseline(_devops_state())

        services = result["discovered_services"]
        assert isinstance(services, list)
        self.assertEqual(
            services,
            [
                ServiceNode(
                    service_id=name,
                    name=name,
                    metadata={
                        "source": "STATIC_CODE",
                        "devops_project": "proj",
                        "repository": "repo-a",
                    },
                )
                for name in ["store-front", "order-service"]
            ],
        )
        edges = result["static_edges"]
        assert isinstance(edges, list)
        self.assertEqual(
            [(e.source_service_id, e.target_service_id) for e in edges],
            [
                ("store-front", "order-service"),
                ("store-front", "product-service"),
                ("order-service", "rabbitmq"),
            ],
        )


if __name__ == "__main__":
    unittest.main()

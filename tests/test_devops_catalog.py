"""Tests for listing Azure DevOps projects and repositories for onboarding (T4e)."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src import api
from src.core import config
from src.tools import devops_catalog
from src.tools.devops_catalog import DevOpsCatalog

ORG = "https://dev.azure.com/acme"


def _catalog(connection: MagicMock) -> DevOpsCatalog:
    with patch.object(devops_catalog, "DefaultAzureCredential"):
        catalog = DevOpsCatalog(ORG)
    catalog._connection = lambda: connection  # type: ignore[method-assign]
    return catalog


class CatalogTests(unittest.TestCase):
    def test_projects_sorted_by_name(self) -> None:
        connection = MagicMock()
        connection.clients.get_core_client.return_value.get_projects.return_value = [
            SimpleNamespace(name="payments", description=None), SimpleNamespace(name="Billing", description="b"),
        ]
        names = [p.name for p in _catalog(connection).projects()]
        self.assertEqual(names, ["Billing", "payments"])

    def test_repositories_report_disabled_and_empty(self) -> None:
        connection = MagicMock()
        connection.clients.get_git_client.return_value.get_repositories.return_value = [
            SimpleNamespace(name="orders", default_branch="refs/heads/main", size=1024, is_disabled=False),
            SimpleNamespace(name="archive", default_branch="refs/heads/master", size=0, is_disabled=True),
            SimpleNamespace(name="blank", default_branch=None, size=0, is_disabled=False),
        ]
        repositories = {r.name: r for r in _catalog(connection).repositories(" shop ")}
        connection.clients.get_git_client.return_value.get_repositories.assert_called_once_with("shop")
        self.assertEqual((repositories["orders"].default_branch, repositories["orders"].size_bytes), ("main", 1024))
        self.assertTrue(repositories["archive"].disabled)
        self.assertTrue(repositories["blank"].empty)
        with self.assertRaises(ValueError):
            _catalog(connection).repositories(" ")

    def test_allowlist_is_enforced(self) -> None:
        with patch.object(config, "ADO_ORG_ALLOWLIST", frozenset({"https://dev.azure.com/other"})), self.assertRaises(ValueError):
            DevOpsCatalog(ORG)


class CatalogApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app)

    def test_endpoints(self) -> None:
        with patch.object(api, "DevOpsCatalog") as catalog_cls:
            catalog_cls.return_value.projects.return_value = [devops_catalog.DevOpsProjectInfo(name="shop")]
            catalog_cls.return_value.repositories.return_value = [devops_catalog.DevOpsRepositoryInfo(name="orders")]
            projects = self.client.post("/api/discover/devops/projects", json={"devops_org_url": ORG})
            repositories = self.client.post(
                "/api/discover/devops/repositories", json={"devops_org_url": ORG, "devops_project": "shop"}
            )
        self.assertEqual(projects.json(), [{"name": "shop", "description": ""}])
        self.assertEqual(repositories.json()[0]["name"], "orders")
        catalog_cls.return_value.repositories.assert_called_once_with("shop")

    def test_failures_map_to_clear_statuses(self) -> None:
        for error, code in [(ValueError("not allowed"), 422), (TimeoutError(), 504), (RuntimeError("401"), 502)]:
            with self.subTest(error=error), patch.object(api, "DevOpsCatalog") as catalog_cls:
                catalog_cls.return_value.projects.side_effect = error
                response = self.client.post("/api/discover/devops/projects", json={"devops_org_url": ORG})
                self.assertEqual(response.status_code, code)


if __name__ == "__main__":
    unittest.main()

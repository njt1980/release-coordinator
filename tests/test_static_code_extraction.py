import unittest
from typing import Any
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.models import (
    DiscoveryMethod,
    DiscoveryResult,
    DiscoveryStageReport,
    DiscoveryStatus,
    ScannedRevision,
    ServiceNode,
    UnresolvedEntityKind,
)
from src.discovery_engine import nodes as discovery_nodes
from src.discovery_engine.state import DiscoveryGraphState
from src.tools import devops_extractor
from src.tools.devops_extractor import (
    BoundarySource,
    DevOpsExtractor,
    EndpointReference,
    ReferenceKind,
    RepositoryScan,
    ServiceBoundary,
    ServiceScan,
)

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


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


class ReleaseCoordManifestTests(unittest.TestCase):
    def test_valid_manifest_normalizes_roots(self) -> None:
        manifest = devops_extractor.parse_release_coord_manifest(
            "services:\n"
            "  - name: payments\n"
            "    root: /services/payments-api/\n"
            "  - name: gateway\n"
            "    root: ./edge/\n"
            "  - name: worker\n"
        )
        self.assertEqual(
            [(s.name, s.root) for s in manifest.services],
            [("payments", "services/payments-api"), ("gateway", "edge"), ("worker", "")],
        )

    def test_invalid_manifests_are_rejected(self) -> None:
        for content in [
            "services: [",  # not YAML
            "",  # empty document
            "services: []",
            "services:\n  - name: Payments_API\n",
            "services:\n  - name: a\n    root: ../other\n",
            "services:\n  - name: a\n  - name: a\n    root: x\n",
            "services:\n  - name: a\n    root: x\n  - name: b\n    root: /x/\n",
            "services:\n  - name: a\n    language: go\n",
            "services:\n  - name: a\n    root: .\n  - name: b\n",  # both at root
            "service:\n  - name: a\n",
        ]:
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    devops_extractor.parse_release_coord_manifest(content)


class InferServiceBoundariesTests(unittest.TestCase):
    def _inferred(self, paths: list[str], repo: str = "Repo_A") -> list[tuple[str, str]]:
        return [
            (b.name, b.root)
            for b in devops_extractor._infer_service_boundaries(paths, repo)
        ]

    def test_root_and_immediate_subdirectories_with_markers(self) -> None:
        self.assertEqual(
            self._inferred(
                [
                    "/go.mod",
                    "/payments/Dockerfile",
                    "/Web_UI/package.json",
                    "/src/order-service/package.json",
                    "/src/order-service/Dockerfile",
                    "/tools/scripts/package.json",  # too deep, not a container
                    "/node_modules/package.json",  # excluded directory
                    "/notes/README.md",  # no marker
                ]
            ),
            [
                ("repo-a", ""),
                ("payments", "payments"),
                ("web-ui", "Web_UI"),
                ("order-service", "src/order-service"),
            ],
        )

    def test_files_belong_to_the_deepest_boundary(self) -> None:
        boundaries = devops_extractor._infer_service_boundaries(
            ["/Dockerfile", "/payments/Dockerfile"], "repo-a"
        )
        owner = devops_extractor._owning_boundary
        self.assertEqual(owner("/payments/app/main.py", boundaries).name, "payments")  # type: ignore[union-attr]
        self.assertEqual(owner("/config.yaml", boundaries).name, "repo-a")  # type: ignore[union-attr]
        self.assertEqual(owner("/paymentsx/a.py", boundaries).name, "repo-a")  # type: ignore[union-attr]
        self.assertIsNone(owner("/a.py", boundaries[1:]))


class NoiseEndpointTests(unittest.TestCase):
    def test_noise_urls_are_dropped(self) -> None:
        for url in [
            "http://localhost:8080/api",
            "http://127.0.0.1:5000",
            "http://0.0.0.0:80",
            "http://[::1]:80",
            "http://x.x.x.x:3000",
            "http://${storefrontip",
            "https://schemas.xmlsoap.org/soap",
            "https://github.com/Azure-Samples/aks-store-demo",
            "https://paypal.me/someone",
            "https://eslint.org/docs/rules",
            "https://kubernetes.default.svc/api",
            "ftp://order-service/file",
        ]:
            with self.subTest(url=url):
                self.assertTrue(devops_extractor._is_noise_endpoint(url))

    def test_dependency_urls_are_kept(self) -> None:
        for url in [
            "http://inventory-service:8080",
            "http://order-service:3000/order",
            "http://product-service.default.svc.cluster.local:3002",
            "http://makeline-service.default.svc",
            "http://10.0.0.12:8080",
            "https://203.0.113.9/v1",
            "http://[fd00::12]:8443",
            "https://payments.internal.example.com/v1",
            "https://vault.corp.internal:8200",
            "https://mydb.privatelink.database.windows.net",
        ]:
            with self.subTest(url=url):
                self.assertFalse(devops_extractor._is_noise_endpoint(url))


class EnvironmentVariableExtractionTests(unittest.TestCase):
    def test_endpoint_variables_are_extracted_across_languages(self) -> None:
        for source, expected in [
            ("url = os.getenv('SERVICE_URL')", ["SERVICE_URL"]),
            ('url = os.environ["ORDER_API"]', ["ORDER_API"]),
            ("url = os.environ.get('INVENTORY_HOST', 'x')", ["INVENTORY_HOST"]),
            ("from os import getenv\nu = getenv('A_URL')", ["A_URL"]),
            ("fetch(process.env.PAYMENTS_API + '/pay')", ["PAYMENTS_API"]),
            ("const u = process.env['ORDER_SERVICE_URL']", ["ORDER_SERVICE_URL"]),
            ("const u = import.meta.env.VITE_API_BASE_URL", ["VITE_API_BASE_URL"]),
            ("const u = process.env.orderServiceUrl", ["orderServiceUrl"]),
            ('u := os.Getenv("MAKELINE_ENDPOINT")', ["MAKELINE_ENDPOINT"]),
            ('u, ok := os.LookupEnv("RABBITMQ_ADDR")', ["RABBITMQ_ADDR"]),
            ('String u = System.getenv("BILLING_URI");', ["BILLING_URI"]),
            (
                'var u = Environment.GetEnvironmentVariable("CATALOG_URL");',
                ["CATALOG_URL"],
            ),
            ("u = ENV.fetch('SEARCH_URL')", ["SEARCH_URL"]),
            ("$u = $_ENV['DATABASE_DSN'];", ["DATABASE_DSN"]),
        ]:
            with self.subTest(source=source):
                self.assertEqual(
                    devops_extractor._extract_env_var_names(source), expected
                )

    def test_non_endpoint_variables_are_ignored(self) -> None:
        source = (
            "os.getenv('API_KEY')\n"
            "process.env.NODE_ENV\n"
            "process.env.PORT\n"
            'os.Getenv("LOG_LEVEL")\n'
            "settings.getenv('OTHER_URL')\n"  # not an environment read
        )
        self.assertEqual(devops_extractor._extract_env_var_names(source), [])

    def test_variables_are_unique_in_source_order(self) -> None:
        source = (
            "a = process.env.B_URL\n"
            "b = os.getenv('A_URL')\n"
            "c = process.env.B_URL\n"
        )
        self.assertEqual(
            devops_extractor._extract_env_var_names(source), ["B_URL", "A_URL"]
        )


def _extractor_with_files(
    files: dict[str, str],
    default_branch: str | None = "refs/heads/main",
    head_sha: str = HEAD_SHA,
) -> DevOpsExtractor:
    with patch.object(devops_extractor, "DefaultAzureCredential"):
        extractor = DevOpsExtractor(
            org_url="https://dev.azure.com/org", project="proj", repositories=[]
        )
    git_client = MagicMock()
    git_client.get_repositories.return_value = [
        SimpleNamespace(name="repo-a", id="id-a", default_branch=default_branch)
    ]
    # A prefix filter also returns longer branch names; only the exact ref counts.
    git_client.get_refs.return_value = [
        SimpleNamespace(name="refs/heads/main-old", object_id="f" * 40),
        SimpleNamespace(name="refs/heads/main", object_id=head_sha),
    ]
    git_client.get_items.return_value = [
        SimpleNamespace(path=path, is_folder=False) for path in files
    ]
    git_client.get_item_content.side_effect = (
        lambda _repo_id, path, project, version_descriptor=None: files[path].encode()
    )
    extractor._get_clients = MagicMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), git_client, "proj")
    )
    return extractor


def _services(result: DiscoveryResult[RepositoryScan]) -> list[ServiceScan]:
    return [scan for repository in result.items for scan in repository.services]


def _summary(
    result: DiscoveryResult[RepositoryScan],
) -> list[tuple[str, list[tuple[str, str]]]]:
    return [
        (
            scan.service.name,
            [(r.kind.value, r.endpoint) for r in scan.references],
        )
        for scan in _services(result)
    ]


class ScanRepositoryTests(unittest.TestCase):
    def test_inferred_services_attribute_dependencies(self) -> None:
        extractor = _extractor_with_files(
            {
                "/README.md": "http://order-service:3000",
                "/docs/api.yaml": "http://order-service:3000",
                "/config.yaml": "url: http://order-service:3000\n",
                "/src/store-front/package.json": "{}",
                "/src/store-front/src/api.js": (
                    "const a = 'http://order-service:3000/order'\n"
                    "const b = 'http://product-service:3002/'\n"
                    "const c = 'https://github.com/x'\n"
                    "const d = 'https://payments.internal.example.com/v1'\n"
                    "const e = process.env.AI_SERVICE_URL\n"
                    "const f = process.env.NODE_ENV\n"
                ),
                "/src/store-front/src/other.js": "'http://order-service:3000/order'",
                "/src/store-front/node_modules/x/index.js": "http://evil:1/",
                "/src/order-service/Dockerfile": "FROM node",
                "/src/order-service/app.js": "http://x.x.x.x:5672 http://10.0.0.12:5672",
                "/src/idle-service/Dockerfile": "FROM node",
                "/src/idle-service/app.js": "console.log('no deps')",
                "/src/unmarked/app.js": "http://order-service:3000",
            }
        )

        result = extractor.scan_repository("repo-a")

        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        # idle-service calls nothing but is still a service; /src/unmarked has
        # no marker file, so it is not a service.
        self.assertEqual(
            _summary(result),
            [
                (
                    "store-front",
                    [
                        ("URL", "http://order-service:3000/order"),
                        ("URL", "http://product-service:3002/"),
                        ("URL", "https://payments.internal.example.com/v1"),
                        ("ENV_VAR", "AI_SERVICE_URL"),
                    ],
                ),
                ("order-service", [("URL", "http://10.0.0.12:5672")]),
                ("idle-service", []),
            ],
        )
        self.assertEqual(
            {s.service.source for s in _services(result)}, {BoundarySource.MARKER}
        )

    def test_manifest_defines_boundaries_and_registers_idle_services(self) -> None:
        extractor = _extractor_with_files(
            {
                "/release-coord.yaml": (
                    "services:\n"
                    "  - name: payments\n"
                    "    root: backend/payments\n"
                    "  - name: ledger\n"
                    "    root: backend/ledger\n"
                    "  - name: edge\n"
                    "    root: .\n"
                ),
                # Markers are ignored when a manifest exists.
                "/backend/payments/Dockerfile": "FROM python",
                "/backend/payments/app/client.py": (
                    "requests.get(os.getenv('LEDGER_URL'))\n"
                    "requests.get('http://10.1.2.3:9000/fx')\n"
                ),
                "/backend/ledger/main.go": "package main",
                "/gateway.conf": "proxy_pass http://payments:8080;",
                "/tools/misc/package.json": "{}",
            }
        )

        result = extractor.scan_repository("repo-a")

        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        self.assertEqual(
            _summary(result),
            [
                (
                    "payments",
                    [("URL", "http://10.1.2.3:9000/fx"), ("ENV_VAR", "LEDGER_URL")],
                ),
                ("ledger", []),
                ("edge", [("URL", "http://payments:8080")]),
            ],
        )
        self.assertEqual(
            [(s.service.root, s.service.source) for s in _services(result)],
            [
                ("backend/payments", BoundarySource.MANIFEST),
                ("backend/ledger", BoundarySource.MANIFEST),
                ("", BoundarySource.MANIFEST),
            ],
        )

    def test_manifest_only_repository_is_still_a_service(self) -> None:
        extractor = _extractor_with_files(
            {
                "/release-coord.yaml": "services:\n  - name: batch-jobs\n",
                "/jobs/nightly.py": "print('no http')",
            }
        )
        result = extractor.scan_repository("repo-a")
        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        self.assertEqual(_summary(result), [("batch-jobs", [])])

    def test_invalid_manifest_fails_the_scan(self) -> None:
        extractor = _extractor_with_files(
            {
                "/release-coord.yaml": "services:\n  - name: Bad_Name\n",
                "/src/a/Dockerfile": "FROM x",
                "/src/a/x.js": "http://b:1",
            }
        )
        with self.assertLogs(devops_extractor.logger, level="WARNING"):
            result = extractor.scan_repository("repo-a")
        self.assertEqual(result.status, DiscoveryStatus.FAILED)
        self.assertEqual(result.items, [])
        self.assertIn("repo-a: invalid release-coord.yaml", result.errors[0])

    def test_scan_records_and_pins_the_default_branch_head(self) -> None:
        extractor = _extractor_with_files(
            {"/release-coord.yaml": "services:\n  - name: api\n", "/app.py": "x"},
            head_sha=HEAD_SHA.upper(),
        )

        result = extractor.scan_repository("repo-a")

        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        self.assertEqual(
            result.items[0].revision,
            ScannedRevision(repository="repo-a", branch="main", commit_sha=HEAD_SHA),
        )
        git_client = extractor._get_clients()[1]  # type: ignore[operator]
        git_client.get_refs.assert_called_once_with(
            "id-a", project="proj", filter="heads/main"
        )
        listing = git_client.get_items.call_args.kwargs["version_descriptor"]
        self.assertEqual((listing.version, listing.version_type), (HEAD_SHA, "commit"))
        reads = git_client.get_item_content.call_args_list
        self.assertEqual(len(reads), 2)
        for call in reads:
            descriptor = call.kwargs["version_descriptor"]
            self.assertEqual(
                (descriptor.version, descriptor.version_type), (HEAD_SHA, "commit")
            )

    def test_repository_without_a_head_commit_fails(self) -> None:
        cases = {
            "no default branch": _extractor_with_files({"/a.py": "x"}, default_branch=None),
            "branch without commit": _extractor_with_files(
                {"/a.py": "x"}, default_branch="refs/heads/trunk"
            ),
        }
        for name, extractor in cases.items():
            with self.subTest(name), self.assertLogs(
                devops_extractor.logger, level="WARNING"
            ):
                result = extractor.scan_repository("repo-a")
                self.assertEqual(result.status, DiscoveryStatus.FAILED)
                self.assertEqual(result.items, [])
                self.assertIn("repo-a", result.errors[0])
                git_client = extractor._get_clients()[1]  # type: ignore[operator]
                git_client.get_items.assert_not_called()

    def test_missing_repository_is_failed_not_empty(self) -> None:
        extractor = _extractor_with_files({})
        result = extractor.scan_repository("missing")
        self.assertEqual(result.status, DiscoveryStatus.FAILED)
        self.assertEqual(result.items, [])
        self.assertIn("missing", result.errors[0])

    def test_scan_exception_is_failed(self) -> None:
        extractor = _extractor_with_files({"/src/a/x.js": "http://b:1"})
        extractor._get_clients.side_effect = RuntimeError("401")  # type: ignore[attr-defined]
        with self.assertLogs(devops_extractor.logger, level="ERROR"):
            result = extractor.scan_repository("repo-a")
        self.assertEqual(result.status, DiscoveryStatus.FAILED)
        self.assertIn("401", result.errors[0])


# FQURI prefix of a statically registered service: onboarded environment and
# cluster, namespace not yet known.
UNKNOWN_URN = "urn:release-coord:default:prod:aks-1:unknown"
REVISION_A = ScannedRevision(repository="repo-a", branch="main", commit_sha=HEAD_SHA)


def _devops_state() -> DiscoveryGraphState:
    return {
        "tenant": "default",
        "project_id": "default",
        "environment": "prod",
        "cluster": "aks-1",
        "workspace_id": "",
        "devops_org_url": "https://dev.azure.com/org",
        "devops_project": "proj",
        "target_repositories": ["repo-a", "repo-b"],
        "target_namespaces": [],
        "discovered_services": [],
        "static_edges": [],
        "runtime_edges": [],
        "discovery_reports": [],
        "scanned_revisions": [],
        "reconciled_graph": {},
        "baseline_snapshot_id": "",
    }


def _scan(
    service: str, *references: tuple[str, ReferenceKind], source: BoundarySource = BoundarySource.MARKER
) -> ServiceScan:
    return ServiceScan(
        service=ServiceBoundary(name=service, root=f"src/{service}", source=source),
        references=[
            EndpointReference(
                service_name=service,
                endpoint=endpoint,
                file_path=f"/src/{service}/a.js",
                kind=kind,
            )
            for endpoint, kind in references
        ],
    )


def _repository_result(*scans: ServiceScan) -> DiscoveryResult[RepositoryScan]:
    return DiscoveryResult[RepositoryScan].success(
        [RepositoryScan(revision=REVISION_A, services=list(scans))]
    )


URL = ReferenceKind.URL
ENV = ReferenceKind.ENV_VAR


class StaticBaselineServiceRegistrationTests(unittest.TestCase):
    def test_edges_originate_from_microservices_not_repositories(self) -> None:
        with patch.object(discovery_nodes, "DevOpsExtractor") as devops_cls:
            devops_cls.return_value.scan_repository.side_effect = [
                _repository_result(
                    _scan(
                        "store-front",
                        ("http://order-service:3000/order", URL),
                        ("http://product-service:3002/", URL),
                        ("AI_SERVICE_URL", ENV),
                    ),
                    _scan(
                        "order-service",
                        ("http://rabbitmq:5672", URL),
                        ("http://10.0.0.12:5432", URL),
                    ),
                    _scan("ledger", source=BoundarySource.MANIFEST),
                ),
                DiscoveryResult[RepositoryScan].failed("repo-b: not found"),
            ]
            result = discovery_nodes.extract_static_code_baseline(_devops_state())

        services = result["discovered_services"]
        assert isinstance(services, list)
        self.assertEqual(
            services,
            [
                ServiceNode(
                    service_id=f"{UNKNOWN_URN}:{name}",
                    name=name,
                    metadata={
                        "source": "STATIC_CODE",
                        "devops_project": "proj",
                        "repository": "repo-a",
                        "service_root": f"src/{name}",
                    },
                )
                # ledger has no dependencies but is still in the inventory.
                for name in ["store-front", "order-service", "ledger"]
            ],
        )
        edges = result["static_edges"]
        assert isinstance(edges, list)
        self.assertEqual(
            [(e.source_service_id, e.target_service_id) for e in edges],
            [
                (f"{UNKNOWN_URN}:store-front", "order-service"),
                (f"{UNKNOWN_URN}:store-front", "product-service"),
                (f"{UNKNOWN_URN}:store-front", "env:store-front/AI_SERVICE_URL"),
                (f"{UNKNOWN_URN}:order-service", "rabbitmq"),
                (f"{UNKNOWN_URN}:order-service", "10.0.0.12"),
            ],
        )
        self.assertEqual({e.provenance for e in edges}, {"azure_devops"})
        # Only the repository that was actually scanned has a revision.
        self.assertEqual(result["scanned_revisions"], [REVISION_A])
        # repo-b failed, so the stage is PARTIAL rather than a clean SUCCESS.
        self.assertEqual(
            result["discovery_reports"],
            [
                DiscoveryStageReport(
                    source=DiscoveryMethod.STATIC_CODE,
                    status=DiscoveryStatus.PARTIAL,
                    item_count=5,
                    errors=["repo-b: not found"],
                )
            ],
        )

    def test_env_and_private_targets_reconcile_to_unresolved_entities(self) -> None:
        state = _devops_state()
        # One repository: the mock returns the same scan for every repository,
        # and two repositories declaring one service would be ambiguous.
        state["target_repositories"] = ["repo-a"]
        with patch.object(discovery_nodes, "DevOpsExtractor") as devops_cls:
            devops_cls.return_value.scan_repository.return_value = _repository_result(
                _scan(
                    "store-front",
                    ("http://ledger:8080", URL),
                    ("https://payments.internal.example.com/v1", URL),
                    ("http://10.0.0.12:5432", URL),
                    ("AI_SERVICE_URL", ENV),
                ),
                _scan("ledger", source=BoundarySource.MANIFEST),
            )
            state.update(discovery_nodes.extract_static_code_baseline(state))  # type: ignore[typeddict-item]
        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        assert isinstance(graph, dict)

        self.assertEqual(
            [(e.source_service_id, e.target_service_id) for e in graph["edges"]][0],
            (f"{UNKNOWN_URN}:store-front", f"{UNKNOWN_URN}:ledger"),
        )
        self.assertEqual(
            [(u.entity_id, u.kind) for u in graph["unresolved_entities"]],
            [
                ("unresolved:payments.internal.example.com", UnresolvedEntityKind.PRIVATE_DNS),
                ("unresolved:10.0.0.12", UnresolvedEntityKind.IP_ADDRESS),
                (
                    "unresolved:env:store-front/ai_service_url",
                    UnresolvedEntityKind.ENVIRONMENT_VARIABLE,
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()


class CommitComparisonTests(unittest.TestCase):
    """The adapter side of PR verification: full SHAs and every diff page."""

    def _extractor(self, git: MagicMock) -> Any:
        from src.tools.devops_extractor import DevOpsExtractor

        extractor = DevOpsExtractor.__new__(DevOpsExtractor)
        extractor._get_clients = lambda: (MagicMock(), git, "Shop")  # type: ignore[method-assign]
        return extractor

    def _change(self, path: str, folder: bool = False) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(item=SimpleNamespace(path=path, is_folder=folder))

    def test_compare_resolves_full_shas_and_reads_every_page(self) -> None:
        from types import SimpleNamespace

        base, head = "a" * 40, "b" * 40
        git = MagicMock()
        git.get_repositories.return_value = [SimpleNamespace(name="Shop", id="r1")]
        git.get_commit.side_effect = lambda sha, *_a, **_k: SimpleNamespace(
            commit_id=base if base.startswith(sha) else head
        )
        git.get_merge_bases.return_value = [SimpleNamespace(commit_id=base.upper())]
        git.get_commit_diffs.side_effect = [
            SimpleNamespace(
                changes=[self._change("/src/a.js"), self._change("/src", folder=True)],
                all_changes_included=False,
            ),
            # The live API returns changes as raw camelCase dicts.
            SimpleNamespace(
                changes=[
                    {"item": {"path": "/src/b.js", "gitObjectType": "blob"}, "changeType": "edit"},
                    {"item": {"path": "/src", "isFolder": True}, "changeType": "edit"},
                ],
                all_changes_included=True,
            ),
        ]

        result = self._extractor(git).compare_commits("Shop", "aaaaaaa", "bbbbbbb")

        (comparison,) = result.items
        self.assertEqual((comparison.base, comparison.target), (base, head))
        self.assertTrue(comparison.base_is_ancestor)
        self.assertEqual(comparison.changed_paths, ["src/a.js", "src/b.js"])
        self.assertEqual(git.get_commit_diffs.call_args_list[1].kwargs["skip"], 2)

    def test_compare_fails_explicitly(self) -> None:
        git = MagicMock()
        git.get_repositories.return_value = []
        result = self._extractor(git).compare_commits("Shop", "a" * 7, "b" * 7)
        self.assertEqual(result.items, [])
        self.assertIn("repository was not found", result.errors[0])

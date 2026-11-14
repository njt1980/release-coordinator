"""PR 2b-2: static coverage accounting, pipeline variables, Kubernetes shape."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from src.coordinator.coverage import radius_shape_findings, static_coverage_issues
from src.coordinator.nodes import explain_pipeline
from src.core.edge_resolution import pipeline_index, resolve_endpoint
from src.core.models import (
    DiscoveryStatus,
    KnowledgeGraph,
    PipelineVariables,
    ReasonCode,
    RepositoryCoverage,
    ScannedRevision,
    ServiceCoverage,
    ServiceEnvironment,
    ServiceNode,
    ShapeFinding,
    ShapeFindingKind,
    UnresolvedEntity,
    UnresolvedEntityKind,
    UnresolvedReach,
    UnscannedFile,
    UnscannedReason,
    unresolved_entity_id,
)
from src.core.service_ids import IdentityContext, IdentityIndex, env_var_reference
from src.discovery_engine import nodes as discovery_nodes
from src.tools import devops_extractor
from src.tools.devops_extractor import (
    DevOpsExtractor,
    FileClass,
    PipelineCollector,
    classify_path,
    collect_definition_variables,
    collect_yaml_variables,
)
from src.tools.k8s_extractor import KubernetesExtractor
from src.tools.policy_store import Exemptions, FileExemption
from tests.fake_devops import coverage_for

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
REVISION = ScannedRevision(repository="repo-a", branch="main", commit_sha=HEAD_SHA)
CONTEXT = IdentityContext(tenant="acme", environment="prod", cluster="aks-1")
URN = "urn:release-coord:acme:prod:aks-1:shop"


def _id(name: str) -> str:
    return f"{URN}:{name}"


# --- Static coverage --------------------------------------------------------


class ClassifyPathTests(unittest.TestCase):
    def test_classes(self) -> None:
        cases = {
            "/src/api/main.py": FileClass.SCANNED,
            "/src/web/App.vue": FileClass.SCANNED,
            "/src/web/index.tsx": FileClass.SCANNED,
            "/src/products/main.rs": FileClass.UNSUPPORTED,
            "/src/billing/Billing.kt": FileClass.UNSUPPORTED,
            "/src/api/node_modules/x/index.js": FileClass.EXCLUDED,
            "/src/api/package-lock.json": FileClass.EXCLUDED,
            "/src/api/vendor/lib.rs": FileClass.EXCLUDED,
            "/src/api/logo.png": FileClass.NOT_SOURCE,
            "/README.md": FileClass.NOT_SOURCE,
            "/src/api/.env.local": FileClass.SCANNED,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertIs(classify_path(path), expected)


def _extractor(files: dict[str, str | None]) -> DevOpsExtractor:
    """An extractor over one repository; a None file fails to read."""
    with patch.object(devops_extractor, "DefaultAzureCredential"):
        extractor = DevOpsExtractor(org_url="https://dev.azure.com/org", project="proj", repositories=[])
    git_client = MagicMock()
    git_client.get_repositories.return_value = [
        SimpleNamespace(name="repo-a", id="id-a", default_branch="refs/heads/main")
    ]
    git_client.get_refs.return_value = [SimpleNamespace(name="refs/heads/main", object_id=HEAD_SHA)]
    git_client.get_items.return_value = [SimpleNamespace(path=p, is_folder=False) for p in files]

    def read(_repo_id: str, path: str, project: str, version_descriptor: Any = None) -> bytes:
        content = files[path]
        if content is None:
            raise RuntimeError("TF401174: item could not be read")
        return content.encode()

    git_client.get_item_content.side_effect = read
    extractor._get_clients = MagicMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), git_client, "proj")
    )
    return extractor


class ScanCoverageTests(unittest.TestCase):
    FILES: dict[str, str | None] = {
        "/src/api/Dockerfile": "FROM node",
        "/src/api/index.js": "fetch(process.env.ORDERS_URL)",
        "/src/api/README.md": "docs",
        "/src/api/node_modules/dep/index.js": "x",
        "/src/products/Dockerfile": "FROM rust",
        "/src/products/main.rs": "let url = env::var(\"AI_URL\");",
        "/src/products/lib.rs": "",
        "/tools/seed.py": "requests.get('http://orders')",
    }

    def test_each_service_accounts_for_every_file(self) -> None:
        result = _extractor(self.FILES).scan_repository("repo-a")

        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        coverage = result.items[0].coverage()
        self.assertEqual(coverage.commit_sha, HEAD_SHA)
        by_root = {service.root: service for service in coverage.services}
        api = by_root["src/api"]
        self.assertEqual(
            (api.files_scanned, api.files_excluded, api.files_not_source, api.complete),
            (1, 1, 2, True),  # Dockerfile and README are not source
        )
        products = by_root["src/products"]
        self.assertFalse(products.complete)
        self.assertEqual(
            [(f.path, f.reason) for f in products.unscanned],
            [
                ("src/products/main.rs", UnscannedReason.UNSUPPORTED_LANGUAGE),
                ("src/products/lib.rs", UnscannedReason.UNSUPPORTED_LANGUAGE),
            ],
        )
        self.assertEqual(coverage.unowned_source_files, ["tools/seed.py"])

    def test_an_unreadable_file_is_partial_and_unscanned(self) -> None:
        files = {**self.FILES, "/src/api/index.js": None}
        with self.assertLogs(devops_extractor.logger, level="WARNING"):
            result = _extractor(files).scan_repository("repo-a")

        self.assertEqual(result.status, DiscoveryStatus.PARTIAL)
        self.assertIn("src/api/index.js", result.errors[0])
        api = next(s for s in result.items[0].services if s.service.root == "src/api")
        assert api.coverage is not None
        self.assertEqual(
            [(f.path, f.reason) for f in api.coverage.unscanned],
            [("src/api/index.js", UnscannedReason.READ_FAILED)],
        )

    def test_a_head_scan_of_some_roots_lists_no_unowned_files(self) -> None:
        result = _extractor(self.FILES).scan_repository(
            "repo-a", commit=REVISION, only_roots={"src/api"}
        )
        self.assertEqual(result.items[0].unowned_source_files, [])
        self.assertEqual([s.root for s in result.items[0].coverage().services], ["src/api"])


SERVICES = [
    ServiceNode(service_id=_id("api"), name="api", metadata={"repository": "repo-a", "service_root": "src/api"}),
    ServiceNode(
        service_id=_id("products"),
        name="products",
        metadata={"repository": "repo-a", "service_root": "src/products"},
    ),
    ServiceNode(service_id=_id("rabbitmq"), name="rabbitmq"),
]
RUST = [
    UnscannedFile(path="src/products/main.rs", reason=UnscannedReason.UNSUPPORTED_LANGUAGE),
    UnscannedFile(path="src/products/lib.rs", reason=UnscannedReason.UNSUPPORTED_LANGUAGE),
]


def _policy(*rules: FileExemption) -> Any:
    return SimpleNamespace(exemptions=Exemptions(unscanned_files=list(rules)))


class CoverageGatingTests(unittest.TestCase):
    def _graph(self, coverage: list[RepositoryCoverage] | None) -> KnowledgeGraph:
        return KnowledgeGraph(services=SERVICES, edges=[], static_coverage=coverage)

    def test_complete_coverage_has_no_issue(self) -> None:
        issues, exempted = static_coverage_issues(None, self._graph(coverage_for(SERVICES)), "snap")
        self.assertEqual((issues, exempted), ([], []))

    def test_unsupported_files_block_with_the_service_named(self) -> None:
        graph = self._graph(coverage_for(SERVICES, HEAD_SHA, {("repo-a", "src/products"): RUST}))
        issues, _ = static_coverage_issues(None, graph, "snap")
        self.assertEqual([i.code for i in issues], [ReasonCode.STATIC_COVERAGE_INCOMPLETE])
        self.assertIn("products (repo-a/src/products) has 2 source file(s)", issues[0].detail)
        self.assertIn("unsupported language (2 .rs)", issues[0].detail)
        self.assertIn("the baseline 0123456789ab", issues[0].detail)

    def test_reviewed_files_are_exempted(self) -> None:
        graph = self._graph(coverage_for(SERVICES, HEAD_SHA, {("repo-a", "src/products"): RUST}))
        rule = FileExemption(pattern="src/products/**/*.rs", reason="Reviewed: calls ai-service only")
        issues, exempted = static_coverage_issues(_policy(rule), graph, "snap")
        self.assertEqual(issues, [])
        self.assertEqual([e.subject for e in exempted], ["src/products/main.rs", "src/products/lib.rs"])
        self.assertEqual({e.rule for e in exempted}, {"src/products/**/*.rs"})

    def test_the_head_replaces_the_baseline_for_scanned_services(self) -> None:
        graph = self._graph(coverage_for(SERVICES, HEAD_SHA, {("repo-a", "src/products"): RUST}))
        head = RepositoryCoverage(
            repository="repo-a",
            commit_sha="b" * 40,
            services=[
                ServiceCoverage(service_name="products", root="src/products", files_scanned=2),
                ServiceCoverage(
                    service_name="api",
                    root="src/api",
                    unscanned=[UnscannedFile(path="src/api/x.kt", reason=UnscannedReason.UNSUPPORTED_LANGUAGE)],
                ),
            ],
        )
        issues, _ = static_coverage_issues(None, graph, "snap", [head])
        self.assertEqual(len(issues), 1)
        self.assertIn("api (repo-a/src/api)", issues[0].detail)
        self.assertIn("the pull request's head bbbbbbbbbbbb", issues[0].detail)

    def test_a_snapshot_without_coverage_or_a_service_missing_from_it_blocks(self) -> None:
        issues, _ = static_coverage_issues(None, self._graph(None), "snap")
        self.assertIn("predates static coverage accounting", issues[0].detail)

        partial = coverage_for(SERVICES[:1])
        issues, _ = static_coverage_issues(None, self._graph(partial), "snap")
        self.assertIn(f"no coverage record for {_id('products')}", issues[0].detail)

    def test_a_graph_with_no_repository_services_needs_no_coverage(self) -> None:
        graph = KnowledgeGraph(services=SERVICES[2:], edges=[])
        self.assertEqual(static_coverage_issues(None, graph, "snap"), ([], []))

    def test_policy_parses_unscanned_file_rules(self) -> None:
        exemptions = Exemptions.model_validate(
            {"unscanned_files": [{"pattern": "src/products/**", "reason": "Reviewed"}]}
        )
        self.assertTrue(exemptions.unscanned_files[0].matches("src/products/src/main.rs"))
        with self.assertRaises(ValueError):
            Exemptions.model_validate({"unscanned_files": [{"pattern": "/abs", "reason": "x"}]})


# --- Pipeline variables -----------------------------------------------------


PIPELINE_YAML = """\
trigger: [main]
variables:
  ORDERS_URL: http://user:pw@orders:3000/api?token=x
  LOG_LEVEL: debug
  IMAGE_TAG: $(Build.BuildId)
  ${{ if eq(variables.env, 'prod') }}:
    PROD_API: http://prod-api
stages:
  - stage: build
    variables:
      - name: PAYMENTS_HOST
        value: payments
      - group: shared-endpoints
      - template: vars.yml
    jobs:
      - job: compile
        variables:
          CACHE_HOST: redis
        steps:
          - script: echo "##vso[task.setvariable variable=DYNAMIC_URL]http://x"
          - template: steps/build.yml
  - stage: deploy
    variables:
      CACHE_HOST: memcached
"""


class PipelineParsingTests(unittest.TestCase):
    def test_yaml_variables_at_every_level(self) -> None:
        collected = PipelineCollector()
        collect_yaml_variables(PIPELINE_YAML, "pipeline ci (azure-pipelines.yml)", collected)
        result = collected.result("repo-a", HEAD_SHA)

        # Endpoint values kept with credentials, query and user info stripped.
        self.assertEqual(
            result.variables,
            {"ORDERS_URL": "http://orders:3000/api", "PAYMENTS_HOST": "payments"},
        )
        self.assertEqual(result.variable_sources["ORDERS_URL"], ["pipeline ci (azure-pipelines.yml)"])
        self.assertEqual(result.withheld_variables, ["LOG_LEVEL"])
        # Macros, conditionals, script-set names and conflicting values are opaque.
        self.assertEqual(
            result.opaque_variables, ["CACHE_HOST", "DYNAMIC_URL", "IMAGE_TAG", "PROD_API"]
        )
        self.assertEqual(
            result.opaque_sources,
            [
                "pipeline ci (azure-pipelines.yml): template steps/build.yml",
                "pipeline ci (azure-pipelines.yml): template vars.yml",
                "pipeline ci (azure-pipelines.yml): variable group shared-endpoints",
            ],
        )

    def test_invalid_yaml_is_an_opaque_source(self) -> None:
        collected = PipelineCollector()
        collect_yaml_variables("variables: [unclosed", "p", collected)
        self.assertEqual(collected.opaque_sources, {"p: not valid YAML"})

    def test_definition_variables(self) -> None:
        definition = SimpleNamespace(
            variables={
                "API_URL": SimpleNamespace(value="http://api", is_secret=False, allow_override=False),
                "TOKEN": SimpleNamespace(value=None, is_secret=True, allow_override=False),
                "TARGET_HOST": SimpleNamespace(value="a", is_secret=False, allow_override=True),
            },
            variable_groups=[SimpleNamespace(name="prod-vars", id=4)],
        )
        collected = PipelineCollector()
        collect_definition_variables(definition, "pipeline cd", collected)
        result = collected.result("repo-a", HEAD_SHA)
        self.assertEqual(result.variables, {"API_URL": "http://api"})
        self.assertEqual(result.opaque_variables, ["TARGET_HOST", "TOKEN"])
        self.assertEqual(result.opaque_sources, ["pipeline cd: variable group prod-vars"])

    def test_may_define(self) -> None:
        pipeline = PipelineVariables(repository="r", variables={"A_URL": "http://a"}, opaque_variables=["B"])
        self.assertTrue(pipeline.may_define("A_URL"))
        self.assertTrue(pipeline.may_define("B"))
        self.assertFalse(pipeline.may_define("C_URL"))
        self.assertTrue(pipeline.model_copy(update={"opaque_sources": ["g"]}).may_define("C_URL"))
        self.assertTrue(PipelineVariables.unreadable("r", "403").may_define("C_URL"))


def _pipeline_extractor(definitions: list[Any], files: dict[str, str]) -> DevOpsExtractor:
    with patch.object(devops_extractor, "DefaultAzureCredential"):
        extractor = DevOpsExtractor(org_url="https://dev.azure.com/org", project="proj", repositories=[])
    build, git = MagicMock(), MagicMock()
    git.get_repositories.return_value = [SimpleNamespace(name="repo-a", id="id-a", type="TfsGit")]
    build.get_definitions.return_value = [SimpleNamespace(id=d.id) for d in definitions]
    build.get_definition.side_effect = lambda definition_id, project: next(
        d for d in definitions if d.id == definition_id
    )
    git.get_item_content.side_effect = lambda _r, path, project, version_descriptor=None: files[path].encode()
    extractor._get_clients = MagicMock(return_value=(build, git, "proj"))  # type: ignore[method-assign]
    return extractor


def _definition(definition_id: int, yaml_file: str | None, repository_id: str = "id-a") -> Any:
    return SimpleNamespace(
        id=definition_id,
        name=f"p{definition_id}",
        process=SimpleNamespace(yaml_filename=yaml_file),
        repository=SimpleNamespace(id=repository_id),
        variables={},
        variable_groups=[],
    )


class ReadPipelineVariablesTests(unittest.TestCase):
    def test_reads_yaml_at_the_scanned_commit(self) -> None:
        extractor = _pipeline_extractor(
            [_definition(1, "/azure-pipelines.yml"), _definition(2, "/ci.yml", repository_id="id-other")],
            {"/azure-pipelines.yml": "variables:\n  ORDERS_URL: http://orders\n"},
        )
        result = extractor.read_pipeline_variables("repo-a", REVISION)

        self.assertEqual(result.variables, {"ORDERS_URL": "http://orders"})
        self.assertEqual(result.commit_sha, HEAD_SHA)
        self.assertEqual(result.opaque_sources, ["pipeline p2: YAML in another repository"])
        git = extractor._get_clients()[1]  # type: ignore[operator]
        descriptor = git.get_item_content.call_args.kwargs["version_descriptor"]
        self.assertEqual((descriptor.version, descriptor.version_type), (HEAD_SHA, "commit"))

    def test_failure_is_unreadable_not_empty(self) -> None:
        extractor = _pipeline_extractor([], {})
        build = extractor._get_clients()[0]  # type: ignore[operator]
        build.get_definitions.side_effect = RuntimeError("401")
        with self.assertLogs(devops_extractor.logger, level="WARNING"):
            result = extractor.read_pipeline_variables("repo-a", REVISION)
        self.assertFalse(result.readable)
        self.assertIn("401", result.error)


class PipelineResolutionTests(unittest.TestCase):
    """A variable Kubernetes doesn't set is unset only if no pipeline may set it."""

    def _resolve(self, pipeline: PipelineVariables | None) -> UnresolvedEntityKind:
        context = CONTEXT.in_namespace("shop")
        index = IdentityIndex(s.identity for s in SERVICES)
        environments = {_id("api"): ServiceEnvironment(service_id=_id("api"))}
        pipelines = pipeline_index(SERVICES, [pipeline] if pipeline else [])
        unresolved: dict[str, UnresolvedEntity] = {}
        entity_id = resolve_endpoint(
            env_var_reference("api", "ORDERS_URL"), index, context, environments, unresolved, pipelines
        )
        return unresolved[entity_id].kind

    def test_unset_without_a_pipeline_definition(self) -> None:
        self.assertIs(self._resolve(None), UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET)
        other = PipelineVariables(repository="repo-a", variables={"OTHER_URL": "http://x"})
        self.assertIs(self._resolve(other), UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET)

    def test_a_pipeline_that_may_set_it_keeps_it_unresolved(self) -> None:
        sets_it = PipelineVariables(repository="repo-a", variables={"ORDERS_URL": "http://orders"})
        self.assertIs(self._resolve(sets_it), UnresolvedEntityKind.ENVIRONMENT_VARIABLE)
        unreadable = PipelineVariables.unreadable("repo-a", "403")
        self.assertIs(self._resolve(unreadable), UnresolvedEntityKind.ENVIRONMENT_VARIABLE)

    def test_the_gap_detail_cites_the_pipeline(self) -> None:
        reference = env_var_reference("api", "ORDERS_URL").lower()
        graph = KnowledgeGraph(
            services=SERVICES,
            edges=[],
            unresolved_entities=[
                UnresolvedEntity(
                    entity_id=unresolved_entity_id(reference),
                    reference=reference,
                    kind=UnresolvedEntityKind.ENVIRONMENT_VARIABLE,
                )
            ],
            pipeline_variables=[
                PipelineVariables(
                    repository="repo-a",
                    variables={"ORDERS_URL": "http://orders"},
                    variable_sources={"ORDERS_URL": ["pipeline ci (azure-pipelines.yml)"]},
                )
            ],
        )
        reach = UnresolvedReach(node=unresolved_entity_id(reference), detail="gap")
        self.assertIn(
            "pipeline ci (azure-pipelines.yml) in repo-a sets ORDERS_URL=http://orders",
            explain_pipeline(reach, graph).detail,
        )


# --- Kubernetes kinds and shape ---------------------------------------------


def _meta(name: str, labels: dict[str, str] | None = None, owner: tuple[str, str] | None = None) -> Any:
    owners = [SimpleNamespace(kind=owner[0], name=owner[1], controller=True)] if owner else None
    return SimpleNamespace(name=name, labels=labels, owner_references=owners)


def _spec(**env: str) -> Any:
    return SimpleNamespace(
        containers=[
            SimpleNamespace(
                name="app",
                env=[SimpleNamespace(name=k, value=v, value_from=None) for k, v in env.items()],
                env_from=None,
            )
        ]
    )


def _workload(name: str, labels: dict[str, str], owner: tuple[str, str] | None = None, **env: str) -> Any:
    template = SimpleNamespace(metadata=_meta("", labels), spec=_spec(**env))
    return SimpleNamespace(metadata=_meta(name, owner=owner), spec=SimpleNamespace(template=template))


def _cron_job(name: str, labels: dict[str, str], **env: str) -> Any:
    template = SimpleNamespace(metadata=_meta("", labels), spec=_spec(**env))
    return SimpleNamespace(
        metadata=_meta(name),
        spec=SimpleNamespace(job_template=SimpleNamespace(spec=SimpleNamespace(template=template))),
    )


def _service(name: str, selector: dict[str, str] | None, kind: str = "ClusterIP", external: str | None = None) -> Any:
    return SimpleNamespace(
        metadata=_meta(name), spec=SimpleNamespace(selector=selector, type=kind, external_name=external)
    )


def _items(*items: Any) -> Any:
    return SimpleNamespace(items=list(items))


def _shape_extractor() -> KubernetesExtractor:
    core, apps, batch = MagicMock(), MagicMock(), MagicMock()
    core.list_namespaced_service.return_value = _items(
        _service("orders", {"app": "orders"}),
        _service("payments-db", None, "ExternalName", "db.payments.example.com."),
        _service("legacy", None),
        _service("search", {"app": "search"}),
        _service("reports", {"app": "reports-web"}),
    )
    core.list_namespaced_config_map.return_value = _items()
    apps.list_namespaced_deployment.return_value = _items(
        _workload("orders", {"app": "orders"}, ORDERS_URL="http://orders"),
        _workload("worker", {"app": "worker"}, ORDERS_URL="http://orders:3000"),
        _workload("reports", {"app": "reports-batch"}),
    )
    apps.list_namespaced_stateful_set.return_value = _items()
    apps.list_namespaced_daemon_set.return_value = _items(
        _workload("log-shipper", {"app": "logs"}, SINK_URL="http://loki:3100")
    )
    apps.list_namespaced_replica_set.return_value = _items(
        _workload("orders-abc", {"app": "orders"}, owner=("Deployment", "orders")),
        _workload("canary-7f", {"app": "canary"}, owner=("Rollout", "canary")),
    )
    batch.list_namespaced_cron_job.return_value = _items(
        _cron_job("nightly", {"app": "nightly"}, REPORT_URL="http://reports")
    )
    batch.list_namespaced_job.return_value = _items(
        _workload("migrate", {"app": "migrate"}),
        _workload("nightly-123", {"app": "nightly"}, owner=("CronJob", "nightly")),
    )
    core.list_namespaced_pod.return_value = _items(
        SimpleNamespace(metadata=_meta("orders-abc-1", {"app": "orders"}, ("ReplicaSet", "orders-abc")), spec=_spec()),
        SimpleNamespace(
            metadata=_meta("canary-7f-1", {"app": "canary"}, ("ReplicaSet", "canary-7f")),
            spec=_spec(TARGET_URL="http://orders"),
        ),
        SimpleNamespace(metadata=_meta("nightly-123-x", {"app": "nightly"}, ("Job", "nightly-123")), spec=_spec()),
    )
    extractor = KubernetesExtractor.__new__(KubernetesExtractor)
    extractor._core_v1_api = core  # type: ignore[assignment]
    extractor._apps_v1_api = apps  # type: ignore[assignment]
    extractor._batch_v1_api = batch  # type: ignore[assignment]
    extractor._api_server = "https://aks-1.example.azmk8s.io:443"
    return extractor


class NamespaceShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        result = _shape_extractor().get_namespace_topology("shop", CONTEXT)
        self.assertEqual(result.status, DiscoveryStatus.SUCCESS)
        (self.topology,) = result.items
        self.findings = {(f.object, f.kind) for f in self.topology.findings}

    def test_unexposed_workloads_become_services_with_their_environment(self) -> None:
        names = {s.name: s.metadata for s in self.topology.services}
        self.assertEqual(
            names["worker"],
            {"source": "K8S_MANIFEST", "workload": "deployment/worker", "exposed": "false"},
        )
        self.assertEqual(names["nightly"]["workload"], "cronjob/nightly")
        self.assertEqual(names["log-shipper"]["workload"], "daemonset/log-shipper")
        self.assertEqual(names["canary"]["workload"], "rollout/canary")
        environments = {e.service_id: e.variables for e in self.topology.environments}
        self.assertEqual(environments[_id("worker")], {"ORDERS_URL": "http://orders:3000"})
        self.assertEqual(environments[_id("nightly")], {"REPORT_URL": "http://reports"})
        self.assertEqual(environments[_id("canary")], {"TARGET_URL": "http://orders"})
        self.assertIn(("deployment/worker", ShapeFindingKind.UNEXPOSED_WORKLOAD), self.findings)

    def test_controllers_we_do_not_read_are_read_from_pods_and_reported(self) -> None:
        self.assertIn(("rollout/canary", ShapeFindingKind.UNREAD_CONTROLLER), self.findings)
        # CronJob- and Deployment-owned pods are covered by their templates.
        self.assertFalse(any(f.object.startswith(("job/", "replicaset/")) and
                             f.kind is ShapeFindingKind.UNREAD_CONTROLLER for f in self.topology.findings))

    def test_transient_jobs_and_name_collisions_are_not_registered(self) -> None:
        names = {s.name for s in self.topology.services}
        self.assertNotIn("migrate", names)
        self.assertIn(("job/migrate", ShapeFindingKind.TRANSIENT_WORKLOAD), self.findings)
        self.assertIn(("deployment/reports", ShapeFindingKind.NAME_COLLISION), self.findings)
        self.assertIn(("service/reports", ShapeFindingKind.SERVICE_SELECTS_NO_WORKLOAD), self.findings)
        self.assertIn(("service/legacy", ShapeFindingKind.SELECTORLESS_SERVICE), self.findings)
        self.assertIn(("service/search", ShapeFindingKind.SERVICE_SELECTS_NO_WORKLOAD), self.findings)

    def test_external_name_services_alias_their_host(self) -> None:
        self.assertEqual(
            [(e.source_service_id, e.target_service_id) for e in self.topology.edges],
            [(_id("payments-db"), "db.payments.example.com")],
        )
        self.assertIn(("service/payments-db", ShapeFindingKind.EXTERNAL_NAME_SERVICE), self.findings)

    def test_discovery_reconciles_the_alias_and_the_worker(self) -> None:
        state: Any = {
            "tenant": "acme",
            "environment": "prod",
            "cluster": "aks-1",
            "target_namespaces": ["shop"],
            "discovered_services": [],
            "discovery_reports": [],
        }
        with patch.object(discovery_nodes, "KubernetesExtractor", return_value=_shape_extractor()):
            state.update(discovery_nodes.extract_k8s_topology(state))
        state["static_edges"] = []
        state["runtime_edges"] = [
            discovery_nodes.DependencyEdge(
                source_service_id="worker",
                target_service_id="orders",
                discovery_methods=[discovery_nodes.DiscoveryMethod.RUNTIME_TELEMETRY],
                confidence_score=0.9,
                last_observed_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
            )
        ]
        graph: Any = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        edges = {(e.source_service_id, e.target_service_id) for e in graph["edges"]}
        self.assertIn((_id("payments-db"), "unresolved:db.payments.example.com"), edges)
        self.assertIn((_id("worker"), _id("orders")), edges)
        self.assertEqual(
            [(e.reference, e.kind) for e in graph["unresolved_entities"]],
            [("db.payments.example.com", UnresolvedEntityKind.EXTERNAL_DNS)],
        )
        self.assertTrue(state["kubernetes_shape"])

    def test_the_cluster_api_service_is_not_a_finding(self) -> None:
        extractor = _shape_extractor()
        extractor._core_v1_api.list_namespaced_service.return_value = _items(  # type: ignore[union-attr]
            _service("kubernetes", None)
        )
        (topology,) = extractor.get_namespace_topology("default", CONTEXT).items
        self.assertFalse(any(f.object == "service/kubernetes" for f in topology.findings))

    def test_a_missing_kind_permission_is_partial(self) -> None:
        extractor = _shape_extractor()
        extractor._batch_v1_api.list_namespaced_cron_job.side_effect = RuntimeError("403")  # type: ignore[union-attr]
        with self.assertLogs("src.tools.k8s_extractor", level="WARNING"):
            result = extractor.get_namespace_topology("shop", CONTEXT)
        self.assertEqual(result.status, DiscoveryStatus.PARTIAL)
        self.assertIn("cron jobs", result.errors[0])

    def test_radius_findings(self) -> None:
        finding = ShapeFinding(
            namespace="shop", object="deployment/worker", kind=ShapeFindingKind.UNEXPOSED_WORKLOAD,
            detail="d", service_id=_id("worker"),
        )
        graph = KnowledgeGraph(services=SERVICES, edges=[], kubernetes_shape=[finding])
        self.assertEqual(radius_shape_findings(graph, [_id("worker")]), [finding])
        self.assertEqual(radius_shape_findings(graph, [_id("api")]), [])


if __name__ == "__main__":
    unittest.main()

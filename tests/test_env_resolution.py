"""Tests for resolving ``env:`` dependencies through Kubernetes manifests."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryStatus,
    KnowledgeGraph,
    ServiceEnvironment,
    ServiceIdentity,
    ServiceNode,
    UnresolvedEntityKind,
)
from src.core.service_ids import (
    IdentityContext,
    IdentityIndex,
    env_var_reference,
    parse_env_var_reference,
    resolve_env_var_reference,
)
from src.discovery_engine import nodes as discovery_nodes
from src.tools.k8s_extractor import KubernetesExtractor

CONTEXT = IdentityContext(tenant="acme")
STATIC = "urn:release-coord:acme:unknown:unknown:unknown"
DEFAULT_NS = "urn:release-coord:acme:unknown:unknown:default"
OTHER_NS = "urn:release-coord:acme:unknown:unknown:other"
S = DiscoveryStatus


# --- Mocked Kubernetes manifests (shaped like kubernetes client objects) ---


def _meta(
    name: str | None = None,
    labels: dict[str, str] | None = None,
    owners: list[object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(name=name, labels=labels, owner_references=owners)


def _env(
    name: str, value: str | None = None, config_map: tuple[str, str] | None = None
) -> SimpleNamespace:
    value_from = None
    if config_map is not None:
        cm_name, key = config_map
        value_from = SimpleNamespace(
            config_map_key_ref=SimpleNamespace(name=cm_name, key=key),
            secret_key_ref=None,
        )
    return SimpleNamespace(name=name, value=value, value_from=value_from)


def _pod_spec(*env: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(containers=[SimpleNamespace(name="app", env=list(env))])


def _workload(name: str, labels: dict[str, str], *env: SimpleNamespace) -> SimpleNamespace:
    template = SimpleNamespace(metadata=_meta(labels=labels), spec=_pod_spec(*env))
    return SimpleNamespace(metadata=_meta(name), spec=SimpleNamespace(template=template))


def _pod(
    name: str, labels: dict[str, str], *env: SimpleNamespace, owned: bool = False
) -> SimpleNamespace:
    owners: list[object] | None = (
        [SimpleNamespace(kind="ReplicaSet")] if owned else None
    )
    return SimpleNamespace(metadata=_meta(name, labels, owners), spec=_pod_spec(*env))


def _service(name: str, selector: dict[str, str] | None) -> SimpleNamespace:
    return SimpleNamespace(metadata=_meta(name), spec=SimpleNamespace(selector=selector))


def _config_map(name: str, data: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(metadata=_meta(name), data=data)


def _items(*items: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(items=list(items))


def _apis(
    services: list[SimpleNamespace],
    deployments: list[SimpleNamespace] | None = None,
    stateful_sets: list[SimpleNamespace] | None = None,
    pods: list[SimpleNamespace] | None = None,
    config_maps: list[SimpleNamespace] | None = None,
) -> tuple[MagicMock, MagicMock]:
    core, apps = MagicMock(), MagicMock()
    core.list_namespaced_service.return_value = _items(*services)
    core.list_namespaced_pod.return_value = _items(*(pods or []))
    core.list_namespaced_config_map.return_value = _items(*(config_maps or []))
    apps.list_namespaced_deployment.return_value = _items(*(deployments or []))
    apps.list_namespaced_stateful_set.return_value = _items(*(stateful_sets or []))
    return core, apps


def _extractor(core: object | None, apps: object | None) -> KubernetesExtractor:
    extractor = KubernetesExtractor.__new__(KubernetesExtractor)
    extractor._core_v1_api = core  # type: ignore[assignment]
    extractor._apps_v1_api = apps  # type: ignore[assignment]
    return extractor


def _order_service_apis() -> tuple[MagicMock, MagicMock]:
    """A namespace where order-service reads its queue host from a config map."""
    return _apis(
        services=[
            _service("order-service", {"app": "order-service"}),
            _service("rabbitmq", {"app": "rabbitmq"}),
        ],
        deployments=[
            _workload(
                "order-service",
                {"app": "order-service", "tier": "backend"},
                _env("ORDER_QUEUE_HOST", config_map=("order-config", "queue_host")),
                _env("ORDER_QUEUE_PORT", value="5672"),
            )
        ],
        stateful_sets=[_workload("rabbitmq", {"app": "rabbitmq"})],
        config_maps=[
            _config_map(
                "order-config", {"queue_host": "rabbitmq.default.svc.cluster.local"}
            )
        ],
    )


class KubernetesEnvironmentExtractionTests(unittest.TestCase):
    def test_reads_literal_and_config_map_values_per_selected_service(self) -> None:
        result = _extractor(*_order_service_apis()).get_namespace_environments(
            "default", CONTEXT
        )

        self.assertEqual(result.status, S.SUCCESS)
        self.assertEqual(
            result.items,
            [
                ServiceEnvironment(
                    service_id=f"{DEFAULT_NS}:order-service",
                    variables={
                        "ORDER_QUEUE_HOST": "rabbitmq.default.svc.cluster.local",
                        "ORDER_QUEUE_PORT": "5672",
                    },
                )
            ],
        )

    def test_stateful_sets_and_standalone_pods_are_read_but_owned_pods_are_not(
        self,
    ) -> None:
        core, apps = _apis(
            services=[_service("ledger", {"app": "ledger"}), _service("job", {"app": "job"})],
            stateful_sets=[_workload("ledger", {"app": "ledger"}, _env("DB_HOST", "pg"))],
            pods=[
                _pod("job", {"app": "job"}, _env("API_URL", "http://ledger:80")),
                _pod("job-abc", {"app": "job"}, _env("API_URL", "http://x:80"), owned=True),
            ],
        )
        result = _extractor(core, apps).get_namespace_environments("default", CONTEXT)

        self.assertEqual(
            {env.service_id: env.variables for env in result.items},
            {
                f"{DEFAULT_NS}:ledger": {"DB_HOST": "pg"},
                f"{DEFAULT_NS}:job": {"API_URL": "http://ledger:80"},
            },
        )

    def test_conflicting_values_for_one_service_are_dropped(self) -> None:
        core, apps = _apis(
            services=[_service("api", {"app": "api"})],
            deployments=[
                _workload("api-blue", {"app": "api"}, _env("DB_HOST", "a"), _env("Q_HOST", "q")),
                _workload("api-green", {"app": "api"}, _env("DB_HOST", "b"), _env("Q_HOST", "q")),
            ],
        )
        with self.assertLogs("src.tools.k8s_extractor", level="WARNING"):
            result = _extractor(core, apps).get_namespace_environments("default", CONTEXT)

        self.assertEqual(result.items[0].variables, {"Q_HOST": "q"})

    def test_missing_config_map_keys_and_selectorless_services_are_skipped(self) -> None:
        core, apps = _apis(
            services=[_service("api", {"app": "api"}), _service("external", None)],
            deployments=[
                _workload(
                    "api",
                    {"app": "api"},
                    _env("DB_HOST", config_map=("missing", "host")),
                    _env("Q_HOST", config_map=("cfg", "absent")),
                    _env("CACHE_HOST", "redis"),
                )
            ],
            config_maps=[_config_map("cfg", {"other": "x"})],
        )
        result = _extractor(core, apps).get_namespace_environments("default", CONTEXT)

        self.assertEqual(result.status, S.SUCCESS)
        self.assertEqual(
            [(env.service_id, env.variables) for env in result.items],
            [(f"{DEFAULT_NS}:api", {"CACHE_HOST": "redis"})],
        )

    def test_unlistable_workload_kind_makes_result_partial(self) -> None:
        core, apps = _order_service_apis()
        apps.list_namespaced_deployment.side_effect = RuntimeError("403")
        with self.assertLogs("src.tools.k8s_extractor", level="WARNING"):
            result = _extractor(core, apps).get_namespace_environments("default", CONTEXT)

        self.assertEqual(result.status, S.PARTIAL)
        self.assertIn("deployments", result.errors[0])

    def test_unlistable_services_or_missing_client_is_failed(self) -> None:
        core, apps = _order_service_apis()
        core.list_namespaced_service.side_effect = RuntimeError("403")
        with self.assertLogs("src.tools.k8s_extractor", level="WARNING"):
            result = _extractor(core, apps).get_namespace_environments("default", CONTEXT)
        self.assertEqual(result.status, S.FAILED)

        missing = _extractor(None, None).get_namespace_environments("default", CONTEXT)
        self.assertEqual(missing.status, S.FAILED)


def _identity(namespace: str, name: str) -> ServiceIdentity:
    return ServiceIdentity(tenant="acme", namespace=namespace, service_name=name)


class EnvReferenceResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = IdentityIndex(
            [
                _identity("default", "order-service"),
                _identity("default", "rabbitmq"),
                _identity("other", "rabbitmq"),
            ]
        )

    def _resolve(self, value: str | None, variable: str = "QUEUE_HOST") -> str | None:
        environments = (
            {f"{DEFAULT_NS}:order-service": {"QUEUE_HOST": value}} if value else {}
        )
        identity = resolve_env_var_reference(
            env_var_reference("order-service", variable),
            environments,
            self.index,
            CONTEXT,
        )
        return identity.uri if identity else None

    def test_parse_env_var_reference(self) -> None:
        self.assertEqual(
            parse_env_var_reference("env:order-service/ORDER_QUEUE_HOST"),
            ("order-service", "ORDER_QUEUE_HOST"),
        )
        for malformed in ["order-service", "env:order-service", "env:/X", "env:a/"]:
            self.assertIsNone(parse_env_var_reference(malformed))

    def test_values_naming_one_registered_service_resolve(self) -> None:
        rabbit = f"{DEFAULT_NS}:rabbitmq"
        for value in [
            "rabbitmq.default.svc.cluster.local",
            "amqp://rabbitmq.default.svc:5672",
            "rabbitmq:5672",  # bare name: the owning service's namespace
        ]:
            with self.subTest(value=value):
                self.assertEqual(self._resolve(value), rabbit)
        self.assertEqual(
            self._resolve("rabbitmq.other.svc.cluster.local"), f"{OTHER_NS}:rabbitmq"
        )

    def test_unknown_variables_or_unmappable_values_do_not_resolve(self) -> None:
        self.assertIsNone(self._resolve(None))
        self.assertIsNone(self._resolve("rabbitmq", variable="queue_host"))
        for value in ["10.0.0.5", "mq.corp.internal", "kafka:9092", "$(MQ_HOST)"]:
            with self.subTest(value=value):
                self.assertIsNone(self._resolve(value))


def _edge(source: str, target: str, method: DiscoveryMethod) -> DependencyEdge:
    return DependencyEdge(
        source_service_id=source,
        target_service_id=target,
        discovery_methods=[method],
        confidence_score=0.7,
        last_observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class ReconcileEnvDependencyTests(unittest.TestCase):
    def _state(self, environments: list[ServiceEnvironment]) -> Any:
        return {
            "tenant": "acme",
            "target_namespaces": ["default"],
            "discovered_services": [
                ServiceNode(
                    service_id=f"{STATIC}:order-service", name="order-service"
                ),
                ServiceNode(
                    service_id=f"{DEFAULT_NS}:order-service", name="order-service"
                ),
                ServiceNode(
                    service_id=f"{DEFAULT_NS}:rabbitmq", name="rabbitmq"
                ),
            ],
            "service_environments": environments,
            "static_edges": [
                _edge(
                    f"{STATIC}:order-service",
                    env_var_reference("order-service", "ORDER_QUEUE_HOST"),
                    DiscoveryMethod.STATIC_CODE,
                ),
                _edge(
                    f"{STATIC}:order-service",
                    env_var_reference("order-service", "PAYMENTS_URL"),
                    DiscoveryMethod.STATIC_CODE,
                ),
            ],
            "runtime_edges": [
                _edge("order-service", "rabbitmq", DiscoveryMethod.RUNTIME_TELEMETRY)
            ],
        }

    def test_env_edges_resolve_to_services_and_unmapped_ones_stay_unresolved(
        self,
    ) -> None:
        state = self._state(
            [
                ServiceEnvironment(
                    service_id=f"{DEFAULT_NS}:order-service",
                    variables={
                        "ORDER_QUEUE_HOST": "rabbitmq.default.svc.cluster.local",
                        "PAYMENTS_URL": "https://payments.example.com",
                    },
                )
            ]
        )
        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        assert isinstance(graph, dict)

        edges = {(e.source_service_id, e.target_service_id): e for e in graph["edges"]}
        resolved = edges[(f"{DEFAULT_NS}:order-service", f"{DEFAULT_NS}:rabbitmq")]
        # The static env edge merged with the runtime edge to the same service.
        self.assertEqual(
            resolved.discovery_methods,
            [DiscoveryMethod.STATIC_CODE, DiscoveryMethod.RUNTIME_TELEMETRY],
        )
        self.assertEqual(
            [(e.entity_id, e.kind) for e in graph["unresolved_entities"]],
            [
                (
                    "unresolved:env:order-service/payments_url",
                    UnresolvedEntityKind.ENVIRONMENT_VARIABLE,
                )
            ],
        )
        KnowledgeGraph(
            services=graph["services"],
            edges=graph["edges"],
            unresolved_entities=graph["unresolved_entities"],
        )

    def test_without_kubernetes_values_env_edges_stay_unresolved(self) -> None:
        graph = discovery_nodes.reconcile_knowledge_graph(self._state([]))[
            "reconciled_graph"
        ]
        assert isinstance(graph, dict)

        self.assertEqual(
            sorted(e.entity_id for e in graph["unresolved_entities"]),
            [
                "unresolved:env:order-service/order_queue_host",
                "unresolved:env:order-service/payments_url",
            ],
        )

    def test_k8s_stage_feeds_reconciliation_from_mocked_manifests(self) -> None:
        state: Any = {
            "tenant": "acme",
            "target_namespaces": ["default"],
            "discovered_services": [
                ServiceNode(
                    service_id=f"{STATIC}:order-service", name="order-service"
                )
            ],
            "static_edges": [
                _edge(
                    f"{STATIC}:order-service",
                    env_var_reference("order-service", "ORDER_QUEUE_HOST"),
                    DiscoveryMethod.STATIC_CODE,
                )
            ],
            "discovery_reports": [],
        }
        with patch.object(
            discovery_nodes,
            "KubernetesExtractor",
            return_value=_extractor(*_order_service_apis()),
        ):
            state.update(discovery_nodes.extract_k8s_topology(state))

        self.assertEqual(state["discovery_reports"][0].status, S.SUCCESS)
        graph = discovery_nodes.reconcile_knowledge_graph(state)["reconciled_graph"]
        assert isinstance(graph, dict)

        self.assertEqual(
            [(e.source_service_id, e.target_service_id) for e in graph["edges"]],
            [(f"{DEFAULT_NS}:order-service", f"{DEFAULT_NS}:rabbitmq")],
        )
        self.assertEqual(graph["unresolved_entities"], [])


if __name__ == "__main__":
    unittest.main()

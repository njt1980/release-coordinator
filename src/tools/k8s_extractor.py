"""Read-only client for extracting Kubernetes service metadata."""

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from src.core.models import DiscoveryResult, ServiceEnvironment, ServiceNode
from src.core.service_ids import IdentityContext
from src.tools._timeout import (
    TIMEOUT_MESSAGE,
    VERIFY_TIMEOUT_SECONDS,
    run_with_timeout,
)


logger = logging.getLogger(__name__)


class KubernetesExtractor:
    """Read-only client boundary for Kubernetes namespace data."""

    def __init__(self) -> None:
        self._core_v1_api: client.CoreV1Api | None = None
        self._apps_v1_api: client.AppsV1Api | None = None
        try:
            config.load_kube_config()  # type: ignore[no-untyped-call]
        except ConfigException:
            try:
                config.load_incluster_config()  # type: ignore[no-untyped-call]
            except ConfigException:
                logger.exception("Unable to load Kubernetes client configuration")
                return
        self._core_v1_api = client.CoreV1Api()
        self._apps_v1_api = client.AppsV1Api()

    def verify_access(self, namespace: str) -> tuple[bool, str]:
        """Confirm read access by listing at most one service in a namespace.

        The check is namespaced because the required RBAC is a namespaced
        Role; a cluster-wide list would fail for correctly scoped identities.

        The request is bounded by VERIFY_TIMEOUT_SECONDS, both via the
        client's own request timeout and an overall deadline.

        Returns:
            (True, "Success") when services are listable, otherwise
            (False, <error message>).
        """
        core_v1_api = self._core_v1_api
        if core_v1_api is None:
            return False, "Kubernetes client configuration could not be loaded"
        try:
            run_with_timeout(
                lambda: core_v1_api.list_namespaced_service(
                    namespace, limit=1, _request_timeout=VERIFY_TIMEOUT_SECONDS
                ),
                VERIFY_TIMEOUT_SECONDS,
            )
            return True, "Success"
        except TimeoutError:
            logger.warning(
                "Kubernetes access verification timed out for namespace %s",
                namespace,
            )
            return False, TIMEOUT_MESSAGE
        except Exception as error:
            logger.warning(
                "Kubernetes access verification failed for namespace %s: %s",
                namespace,
                error,
            )
            return False, str(error)

    def get_namespace_services(
        self, namespace: str, context: IdentityContext
    ) -> DiscoveryResult[ServiceNode]:
        """Return service nodes observed in a Kubernetes namespace.

        This method is intended to read service metadata from the specified
        namespace only. It must not create, update, or delete Kubernetes
        resources. Each node's ``service_id`` is its FQURI, built from
        ``context`` (tenant, environment, cluster) and the namespace.

        Args:
            namespace: Kubernetes namespace whose services to inspect.
            context: Identity segments known for this discovery run.

        Returns:
            SUCCESS with the namespace's services, PARTIAL when some service
            objects were unusable, or FAILED when the namespace could not be
            listed.
        """
        if self._core_v1_api is None:
            logger.error("Kubernetes API client is unavailable")
            return DiscoveryResult[ServiceNode].failed(
                f"{namespace}: Kubernetes client configuration could not be loaded"
            )

        try:
            response = self._core_v1_api.list_namespaced_service(namespace)
        except Exception as error:
            logger.exception(
                "Failed to list Kubernetes services in namespace %s", namespace
            )
            return DiscoveryResult[ServiceNode].failed(
                f"{namespace}: failed to list services: {error}"
            )

        services: list[ServiceNode] = []
        skipped = 0
        for service in response.items:
            metadata = service.metadata
            if not metadata or not metadata.name:
                logger.warning("Skipping Kubernetes service without a name")
                skipped += 1
                continue
            services.append(
                ServiceNode(
                    service_id=context.identity(metadata.name, namespace).uri,
                    name=metadata.name,
                )
            )
        errors = (
            [f"{namespace}: skipped {skipped} service(s) without a name"]
            if skipped
            else []
        )
        return DiscoveryResult[ServiceNode].from_items(services, errors)

    def get_namespace_environments(
        self, namespace: str, context: IdentityContext
    ) -> DiscoveryResult[ServiceEnvironment]:
        """Return the container environment variables of each namespace service.

        Reads Deployments, StatefulSets and standalone Pods (Pods with no
        owner, so ReplicaSet-managed Pods are not counted twice) and maps
        each workload to every Service whose selector matches its pod
        labels. Variables come from ``env[].value``,
        ``env[].valueFrom.configMapKeyRef`` and ``envFrom[].configMapRef``;
        other sources (Secrets, field refs) are not read. A variable with conflicting values
        across one service's containers is dropped rather than guessed.
        Read-only: nothing is created, updated or deleted.

        Returns:
            SUCCESS with one entry per selected service, PARTIAL when some
            resource kinds could not be listed, or FAILED when the client is
            unavailable or the namespace's services could not be listed.
        """
        core_v1_api = self._core_v1_api
        apps_v1_api = self._apps_v1_api
        if core_v1_api is None or apps_v1_api is None:
            return DiscoveryResult[ServiceEnvironment].failed(
                f"{namespace}: Kubernetes client configuration could not be loaded"
            )
        errors: list[str] = []
        services = _list_items(
            lambda: core_v1_api.list_namespaced_service(namespace),
            namespace, "services", errors,
        )
        if services is None:
            return DiscoveryResult[ServiceEnvironment].failed(errors[0])
        config_maps = _config_map_data(
            _list_items(
                lambda: core_v1_api.list_namespaced_config_map(namespace),
                namespace, "config maps", errors,
            )
            or []
        )
        templates: list[tuple[dict[str, str], Any]] = []
        workload_listers: list[tuple[str, Callable[[], Any]]] = [
            ("deployments", lambda: apps_v1_api.list_namespaced_deployment(namespace)),
            (
                "stateful sets",
                lambda: apps_v1_api.list_namespaced_stateful_set(namespace),
            ),
        ]
        for kind, request in workload_listers:
            for item in _list_items(request, namespace, kind, errors) or []:
                if item.spec and item.spec.template:
                    template = item.spec.template
                    templates.append((_labels(template.metadata), template.spec))
        templates.extend(
            (_labels(pod.metadata), pod.spec)
            for pod in _list_items(
                lambda: core_v1_api.list_namespaced_pod(namespace),
                namespace, "pods", errors,
            )
            or []
            if not (pod.metadata and pod.metadata.owner_references)
        )
        environments = _service_environments(
            services, templates, config_maps, namespace, context
        )
        return DiscoveryResult[ServiceEnvironment].from_items(environments, errors)


def _list_items(
    request: Callable[[], Any], namespace: str, kind: str, errors: list[str]
) -> list[Any] | None:
    """Return a list call's items, or None after recording why it failed."""
    try:
        return list(request().items)
    except Exception as error:
        logger.warning("Failed to list Kubernetes %s in %s: %s", kind, namespace, error)
        errors.append(f"{namespace}: failed to list {kind}: {error}")
        return None


def _labels(metadata: Any) -> dict[str, str]:
    """Return an object's labels, empty when it has none."""
    return dict(getattr(metadata, "labels", None) or {})


def _config_map_data(config_maps: Iterable[Any]) -> dict[str, dict[str, str]]:
    """Index config map data by config map name."""
    return {
        item.metadata.name: dict(item.data or {})
        for item in config_maps
        if item.metadata and item.metadata.name
    }


def _env_value(
    env_var: Any, config_maps: Mapping[str, Mapping[str, str]]
) -> str | None:
    """Return a container env var's value when it is literal or from a config map."""
    if env_var.value is not None:
        return str(env_var.value)
    ref = getattr(env_var.value_from, "config_map_key_ref", None)
    if ref is None or not ref.name or not ref.key:
        return None
    return config_maps.get(ref.name, {}).get(ref.key)


def _container_env(
    pod_spec: Any, config_maps: Mapping[str, Mapping[str, str]]
) -> list[tuple[str, str]]:
    """Return the (name, value) env pairs of every container in a pod spec.

    Follows Kubernetes precedence: later ``envFrom`` config maps override
    earlier ones, and explicit ``env`` entries override ``envFrom``. An
    explicit entry whose value cannot be read (e.g. a Secret ref) still
    shadows any ``envFrom`` value of the same name.
    """
    pairs: list[tuple[str, str]] = []
    for container in getattr(pod_spec, "containers", None) or []:
        resolved: dict[str, str] = {}
        for source in getattr(container, "env_from", None) or []:
            ref = getattr(source, "config_map_ref", None)
            if ref is None or not ref.name:
                continue
            prefix = getattr(source, "prefix", None) or ""
            for key, value in config_maps.get(ref.name, {}).items():
                resolved[f"{prefix}{key}"] = value
        for env_var in container.env or []:
            if not env_var.name:
                continue
            value = _env_value(env_var, config_maps)
            if value is None:
                resolved.pop(env_var.name, None)
            else:
                resolved[env_var.name] = value
        pairs.extend(resolved.items())
    return pairs


def _selects(selector: Mapping[str, str], labels: Mapping[str, str]) -> bool:
    """Whether a non-empty Service selector matches a pod's labels."""
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _service_environments(
    services: Iterable[Any],
    templates: list[tuple[dict[str, str], Any]],
    config_maps: Mapping[str, Mapping[str, str]],
    namespace: str,
    context: IdentityContext,
) -> list[ServiceEnvironment]:
    """Build each service's env mapping from the pod templates it selects."""
    environments: list[ServiceEnvironment] = []
    for service in services:
        if not (service.metadata and service.metadata.name):
            continue
        selector = dict(getattr(service.spec, "selector", None) or {})
        values: dict[str, set[str]] = {}
        for labels, pod_spec in templates:
            if _selects(selector, labels):
                for name, value in _container_env(pod_spec, config_maps):
                    values.setdefault(name, set()).add(value)
        if not values:
            continue
        conflicting = sorted(name for name, found in values.items() if len(found) > 1)
        if conflicting:
            logger.warning(
                "Ignoring env vars with conflicting values for service %s/%s: %s",
                namespace, service.metadata.name, conflicting,
            )
        environments.append(
            ServiceEnvironment(
                service_id=context.identity(service.metadata.name, namespace).uri,
                variables={
                    name: next(iter(found))
                    for name, found in values.items()
                    if len(found) == 1
                },
            )
        )
    return environments

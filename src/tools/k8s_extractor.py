"""Read-only client for extracting Kubernetes service metadata."""

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final, NamedTuple

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from src.core.models import (
    DiscoveryResult,
    ServiceEnvironment,
    ServiceNode,
    normalize_api_server,
)
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
        self._api_server = ""
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
        self._api_server = _normalized_server(
            str(client.Configuration.get_default_copy().host or "")
        )

    @property
    def api_server(self) -> str:
        """The API server every request goes to, as ``https://<host>:<port>``.

        Blank when no configuration loaded or the server is not an https
        URL; a blank server never matches a project policy, so the scan
        cannot vouch for any cluster.
        """
        return self._api_server

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
        templates: list[PodTemplate] = []
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
                    templates.append(
                        PodTemplate(
                            labels=_labels(template.metadata),
                            workload=_workload_name(kind, item),
                            spec=template.spec,
                        )
                    )
        templates.extend(
            PodTemplate(
                labels=_labels(pod.metadata),
                workload=_workload_name("pods", pod),
                spec=pod.spec,
            )
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


def _normalized_server(host: str) -> str:
    """Normalize the configured API server, or return "" if it is unusable."""
    try:
        return normalize_api_server(host)
    except ValueError as error:
        logger.warning("Kubernetes API server cannot identify a cluster: %s", error)
        return ""


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


class _ContainerEnv:
    """What one pod spec's containers set.

    ``pairs`` are (variable, value, provenance) for readable values, where
    provenance is ``<container>:<source>``; ``opaque`` names are set but
    unreadable in at least one container; ``has_opaque_sources`` is true
    when some container imports names we can't see.
    """

    def __init__(self) -> None:
        self.pairs: list[tuple[str, str, str]] = []
        self.opaque: set[str] = set()
        self.has_opaque_sources = False


def _env_origin(env_var: Any) -> str:
    """Name where an explicit, readable ``env`` entry's value comes from."""
    ref = getattr(getattr(env_var, "value_from", None), "config_map_key_ref", None)
    if getattr(env_var, "value", None) is None and ref is not None:
        return f"configMapKeyRef/{ref.name}"
    return "env"


def _env_from_source(source: Any, config_maps: Mapping[str, Mapping[str, str]]) -> str | None:
    """Return the config map an ``envFrom`` entry reads, or None if opaque.

    A Secret, or a config map that is unnamed or couldn't be read, may set
    any name with a value we can't see.
    """
    ref = getattr(source, "config_map_ref", None)
    if getattr(source, "secret_ref", None) is not None or ref is None:
        return None
    if not ref.name or ref.name not in config_maps:
        return None
    return str(ref.name)


def _container_env(
    pod_spec: Any, config_maps: Mapping[str, Mapping[str, str]]
) -> _ContainerEnv:
    """Return the environment the containers in a pod spec receive.

    Follows Kubernetes precedence: later ``envFrom`` sources override
    earlier ones, and explicit ``env`` entries override ``envFrom``. An
    ``envFrom`` Secret, or a config map that couldn't be read, may set any
    name, so it marks the spec as having opaque sources, and every config
    map value imported before it in the same container becomes opaque: the
    later source may override it. An explicit entry whose value cannot be
    read (a Secret or field ref, a missing config map key) is opaque and
    still shadows any ``envFrom`` value of the same name.
    """
    env = _ContainerEnv()
    for container in getattr(pod_spec, "containers", None) or []:
        container_name = getattr(container, "name", None) or "?"
        resolved: dict[str, tuple[str, str]] = {}
        opaque: set[str] = set()
        for source in getattr(container, "env_from", None) or []:
            map_name = _env_from_source(source, config_maps)
            if map_name is None:
                env.has_opaque_sources = True
                opaque.update(resolved)
                resolved.clear()
                continue
            prefix = getattr(source, "prefix", None) or ""
            for key, map_value in config_maps[map_name].items():
                name = f"{prefix}{key}"
                resolved[name] = (map_value, f"configMap/{map_name}")
                opaque.discard(name)
        for env_var in container.env or []:
            if not env_var.name:
                continue
            value = _env_value(env_var, config_maps)
            if value is None:
                resolved.pop(env_var.name, None)
                opaque.add(env_var.name)
            else:
                resolved[env_var.name] = (value, _env_origin(env_var))
                opaque.discard(env_var.name)
        env.pairs.extend(
            (name, value, f"{container_name}:{origin}")
            for name, (value, origin) in resolved.items()
        )
        env.opaque.update(opaque)
    return env


def _selects(selector: Mapping[str, str], labels: Mapping[str, str]) -> bool:
    """Whether a non-empty Service selector matches a pod's labels."""
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


class PodTemplate(NamedTuple):
    """A pod spec some workload runs, with its labels and owner.

    ``workload`` is ``<kind>/<name>`` (for example ``deployment/orders``).
    """

    labels: dict[str, str]
    workload: str
    spec: Any


_WORKLOAD_KINDS: Final[dict[str, str]] = {
    "deployments": "deployment",
    "stateful sets": "statefulset",
    "pods": "pod",
}


def _workload_name(kind: str, item: Any) -> str:
    """Return ``<kind>/<name>`` for a listed workload."""
    name = getattr(getattr(item, "metadata", None), "name", None) or "?"
    return f"{_WORKLOAD_KINDS.get(kind, kind)}/{name}"


def _service_environments(
    services: Iterable[Any],
    templates: list[PodTemplate],
    config_maps: Mapping[str, Mapping[str, str]],
    namespace: str,
    context: IdentityContext,
) -> list[ServiceEnvironment]:
    """Build each service's env mapping from the pod templates it selects.

    Every service that selects at least one workload gets an entry, even
    with no variables, so a variable can be proven unset. A variable with
    conflicting values across the service's containers, or set opaquely by
    any of them, is opaque: set, but not to one readable value. Each
    readable value records the workload and container it came from.
    """
    environments: list[ServiceEnvironment] = []
    for service in services:
        if not (service.metadata and service.metadata.name):
            continue
        selector = dict(getattr(service.spec, "selector", None) or {})
        selected = [
            (template.workload, template.spec)
            for template in templates
            if _selects(selector, template.labels)
        ]
        if not selected:
            continue
        values: dict[str, set[str]] = {}
        sources: dict[str, set[str]] = {}
        opaque: set[str] = set()
        has_opaque_sources = False
        for workload, pod_spec in selected:
            env = _container_env(pod_spec, config_maps)
            for name, value, origin in env.pairs:
                values.setdefault(name, set()).add(value)
                sources.setdefault(name, set()).add(f"{workload}/{origin}")
            opaque.update(env.opaque)
            has_opaque_sources = has_opaque_sources or env.has_opaque_sources
        conflicting = sorted(name for name, found in values.items() if len(found) > 1)
        if conflicting:
            logger.warning(
                "Ignoring env vars with conflicting values for service %s/%s: %s",
                namespace, service.metadata.name, conflicting,
            )
        # A name is definite only when every container that sets it sets the
        # same readable value; one opaque setting anywhere makes it opaque.
        definite = {
            name: next(iter(found))
            for name, found in values.items()
            if len(found) == 1 and name not in opaque
        }
        environments.append(
            ServiceEnvironment(
                service_id=context.identity(service.metadata.name, namespace).uri,
                variables=definite,
                variable_sources={name: sorted(sources[name]) for name in definite},
                opaque_variables=sorted((opaque | set(conflicting)) - set(definite)),
                has_opaque_sources=has_opaque_sources,
            )
        )
    return environments

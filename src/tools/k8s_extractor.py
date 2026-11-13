"""Read-only client for extracting Kubernetes service metadata."""

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Final, NamedTuple

from pydantic import BaseModel, Field

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from src.core.models import (
    DependencyEdge,
    DiscoveryMethod,
    DiscoveryResult,
    ServiceEnvironment,
    ServiceNode,
    ShapeFinding,
    ShapeFindingKind,
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
        self._batch_v1_api: client.BatchV1Api | None = None
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
        self._batch_v1_api = client.BatchV1Api()
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

    def get_namespace_topology(
        self, namespace: str, context: IdentityContext
    ) -> DiscoveryResult["NamespaceTopology"]:
        """Return a namespace's services, their environments and its shape.

        Read-only: nothing is created, updated or deleted; Secrets are never
        read. Services become nodes under FQURIs built from ``context``.
        Pod templates come from Deployments, StatefulSets, DaemonSets,
        CronJobs, and Jobs, ReplicaSets and Pods without an owner; pods
        whose controller is another kind (an operator, Argo Rollouts) are
        read from their running specs. Each Service gets the environment
        of the templates it selects (see ``_service_environment``). A
        long-running workload or CronJob that no Service selects is
        registered as a service too (``exposed=false``), so its calls
        count; an ExternalName Service gets a manifest edge to its host.
        Objects outside the supported shape are reported as findings.

        Returns:
            SUCCESS with one ``NamespaceTopology``, PARTIAL when some kinds
            couldn't be listed or a Service has no name, or FAILED when the
            client is unavailable or the Services can't be listed.
        """
        core_v1_api = self._core_v1_api
        apps_v1_api = self._apps_v1_api
        batch_v1_api = self._batch_v1_api
        if core_v1_api is None or apps_v1_api is None or batch_v1_api is None:
            return DiscoveryResult[NamespaceTopology].failed(
                f"{namespace}: Kubernetes client configuration could not be loaded"
            )
        errors: list[str] = []
        services = _list_items(
            lambda: core_v1_api.list_namespaced_service(namespace),
            namespace, "services", errors,
        )
        if services is None:
            return DiscoveryResult[NamespaceTopology].failed(errors[0])

        def listed(kind: str, request: Callable[[], Any]) -> list[Any]:
            return _list_items(request, namespace, kind, errors) or []

        config_maps = _config_map_data(
            listed("config maps", lambda: core_v1_api.list_namespaced_config_map(namespace))
        )
        templates, findings = _pod_templates(
            namespace,
            deployments=listed(
                "deployments", lambda: apps_v1_api.list_namespaced_deployment(namespace)
            ),
            stateful_sets=listed(
                "stateful sets", lambda: apps_v1_api.list_namespaced_stateful_set(namespace)
            ),
            daemon_sets=listed(
                "daemon sets", lambda: apps_v1_api.list_namespaced_daemon_set(namespace)
            ),
            replica_sets=listed(
                "replica sets", lambda: apps_v1_api.list_namespaced_replica_set(namespace)
            ),
            cron_jobs=listed(
                "cron jobs", lambda: batch_v1_api.list_namespaced_cron_job(namespace)
            ),
            jobs=listed("jobs", lambda: batch_v1_api.list_namespaced_job(namespace)),
            pods=listed("pods", lambda: core_v1_api.list_namespaced_pod(namespace)),
        )
        named = [item for item in services if item.metadata and item.metadata.name]
        if len(named) < len(services):
            logger.warning("Skipping Kubernetes service without a name")
            errors.append(
                f"{namespace}: skipped {len(services) - len(named)} service(s) without a name"
            )
        topology = _namespace_topology(namespace, context, named, templates, config_maps)
        topology.findings[:0] = findings
        return DiscoveryResult[NamespaceTopology].from_items([topology], errors)


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


class NamespaceTopology(BaseModel):
    """What one namespace's manifests say, as discovery records it.

    ``services`` are the namespace's Services plus its unexposed workloads;
    ``environments`` the container environment each one's templates
    receive; ``edges`` the dependencies manifests declare (an ExternalName
    alias to its host); ``findings`` the objects outside the supported shape.
    """

    namespace: str
    services: list[ServiceNode] = Field(default_factory=list)
    environments: list[ServiceEnvironment] = Field(default_factory=list)
    edges: list[DependencyEdge] = Field(default_factory=list)
    findings: list[ShapeFinding] = Field(default_factory=list)


MANIFEST_PROVENANCE: Final[str] = "kubernetes"
_EXPOSED_METADATA_KEY: Final[str] = "exposed"
_WORKLOAD_METADATA_KEY: Final[str] = "workload"
# Owner kinds whose pod templates are read directly.
_READ_CONTROLLERS: Final[frozenset[str]] = frozenset(
    {"Deployment", "StatefulSet", "DaemonSet", "CronJob"}
)


class PodTemplate(NamedTuple):
    """A pod spec some workload runs, with its labels and owner.

    ``workload`` is ``<kind>/<name>`` (for example ``deployment/orders``).
    ``registrable`` is true for workloads that run continuously or on a
    schedule (registered as a service when no Service selects them), false
    for bare Jobs and Pods, which come and go.
    """

    labels: dict[str, str]
    workload: str
    spec: Any
    registrable: bool = True


def _name(item: Any) -> str:
    return str(getattr(getattr(item, "metadata", None), "name", None) or "?")


def _owners(item: Any) -> list[Any]:
    return list(getattr(getattr(item, "metadata", None), "owner_references", None) or [])


def _controller(item: Any) -> tuple[str, str] | None:
    """Return the (kind, name) of an object's controlling owner, if any."""
    owners = _owners(item)
    if not owners:
        return None
    owner = next((o for o in owners if getattr(o, "controller", None)), owners[0])
    return str(getattr(owner, "kind", None) or "?"), str(getattr(owner, "name", None) or "?")


def _template(kind: str, item: Any, template: Any, registrable: bool = True) -> PodTemplate | None:
    if template is None:
        return None
    return PodTemplate(
        labels=_labels(getattr(template, "metadata", None)),
        workload=f"{kind}/{_name(item)}",
        spec=getattr(template, "spec", None),
        registrable=registrable,
    )


def _pod_templates(
    namespace: str,
    *,
    deployments: list[Any],
    stateful_sets: list[Any],
    daemon_sets: list[Any],
    replica_sets: list[Any],
    cron_jobs: list[Any],
    jobs: list[Any],
    pods: list[Any],
) -> tuple[list[PodTemplate], list[ShapeFinding]]:
    """Return every pod template the namespace runs, and unread controllers.

    Controllers are followed through ReplicaSets and Jobs: a pod whose
    chain ends at a kind in ``_READ_CONTROLLERS`` (or at an owner-less
    ReplicaSet or Job) is already covered by that template. Any other
    chain ends at a controller we don't read; its running pods are used
    and it is reported as UNREAD_CONTROLLER.
    """
    templates: list[PodTemplate] = []
    for kind, items in (
        ("deployment", deployments),
        ("statefulset", stateful_sets),
        ("daemonset", daemon_sets),
    ):
        for item in items:
            found = _template(kind, item, getattr(getattr(item, "spec", None), "template", None))
            if found:
                templates.append(found)
    for item in cron_jobs:
        job_spec = getattr(getattr(getattr(item, "spec", None), "job_template", None), "spec", None)
        found = _template("cronjob", item, getattr(job_spec, "template", None))
        if found:
            templates.append(found)
    for kind, items, registrable in (("replicaset", replica_sets, True), ("job", jobs, False)):
        for item in items:
            if _controller(item) is None:
                found = _template(
                    kind, item, getattr(getattr(item, "spec", None), "template", None), registrable
                )
                if found:
                    templates.append(found)

    parents = {
        ("ReplicaSet", _name(item)): _controller(item) for item in replica_sets
    } | {("Job", _name(item)): _controller(item) for item in jobs}
    unread: dict[str, list[Any]] = {}
    for pod in pods:
        owner = _controller(pod)
        if owner is None:
            templates.append(
                PodTemplate(_labels(pod.metadata), f"pod/{_name(pod)}", pod.spec, False)
            )
            continue
        top = _top_controller(owner, parents)
        if top is not None:
            unread.setdefault(f"{top[0]}/{top[1]}", []).append(pod)
    findings: list[ShapeFinding] = []
    for controller, owned in sorted(unread.items()):
        templates.extend(
            PodTemplate(_labels(pod.metadata), controller.lower(), pod.spec) for pod in owned
        )
        findings.append(
            ShapeFinding(
                namespace=namespace,
                object=controller.lower(),
                kind=ShapeFindingKind.UNREAD_CONTROLLER,
                detail=(
                    f"{len(owned)} pod(s) belong to a {controller.split('/')[0]}, a "
                    "controller discovery doesn't read; their running specs were "
                    "read instead, so it is invisible while scaled to zero"
                ),
            )
        )
    return templates, findings


def _top_controller(
    owner: tuple[str, str], parents: Mapping[tuple[str, str], tuple[str, str] | None]
) -> tuple[str, str] | None:
    """Follow ReplicaSet and Job owners up; return the controller if unread.

    None when the chain ends at a read controller, at an owner-less
    ReplicaSet or Job (read directly), or at one we didn't list (being
    replaced, or its list failed, which is already an error).
    """
    seen: set[tuple[str, str]] = set()
    while owner[0] in {"ReplicaSet", "Job"} and owner not in seen:
        seen.add(owner)
        if owner not in parents:
            return None
        parent = parents[owner]
        if parent is None:
            return None
        owner = parent
    return None if owner[0] in _READ_CONTROLLERS else owner


def _service_environment(
    service_id: str, selected: list[PodTemplate], config_maps: Mapping[str, Mapping[str, str]]
) -> ServiceEnvironment:
    """Build a service's env mapping from the pod templates it selects.

    A variable with conflicting values across the service's containers, or
    set opaquely by any of them, is opaque: set, but not to one readable
    value. Each readable value records the workload and container it came
    from.
    """
    values: dict[str, set[str]] = {}
    sources: dict[str, set[str]] = {}
    opaque: set[str] = set()
    has_opaque_sources = False
    for template in selected:
        env = _container_env(template.spec, config_maps)
        for name, value, origin in env.pairs:
            values.setdefault(name, set()).add(value)
            sources.setdefault(name, set()).add(f"{template.workload}/{origin}")
        opaque.update(env.opaque)
        has_opaque_sources = has_opaque_sources or env.has_opaque_sources
    conflicting = sorted(name for name, found in values.items() if len(found) > 1)
    if conflicting:
        logger.warning(
            "Ignoring env vars with conflicting values for service %s: %s",
            service_id, conflicting,
        )
    # A name is definite only when every container that sets it sets the
    # same readable value; one opaque setting anywhere makes it opaque.
    definite = {
        name: next(iter(found))
        for name, found in values.items()
        if len(found) == 1 and name not in opaque
    }
    return ServiceEnvironment(
        service_id=service_id,
        variables=definite,
        variable_sources={name: sorted(sources[name]) for name in definite},
        opaque_variables=sorted((opaque | set(conflicting)) - set(definite)),
        has_opaque_sources=has_opaque_sources,
    )


def _finding(
    namespace: str, obj: str, kind: ShapeFindingKind, detail: str, service_id: str | None
) -> ShapeFinding:
    return ShapeFinding(
        namespace=namespace, object=obj, kind=kind, detail=detail, service_id=service_id
    )


def _namespace_topology(
    namespace: str,
    context: IdentityContext,
    services: list[Any],
    templates: list[PodTemplate],
    config_maps: Mapping[str, Mapping[str, str]],
) -> NamespaceTopology:
    """Map Services and workloads to service nodes, environments and findings.

    Every Service that selects a template gets an environment entry, even
    with no variables, so a variable can be proven unset. Services that
    can't (ExternalName, selectorless, selecting nothing) are still
    registered, with a finding saying why their environment is unknown.
    """
    topology = NamespaceTopology(namespace=namespace)
    selected_workloads: set[str] = set()
    service_names: set[str] = set()
    now = datetime.now(timezone.utc)
    for service in services:
        name = str(service.metadata.name)
        service_names.add(name)
        service_id = context.identity(name, namespace).uri
        topology.services.append(ServiceNode(service_id=service_id, name=name))
        spec = getattr(service, "spec", None)
        external = str(getattr(spec, "external_name", None) or "").strip().rstrip(".").lower()
        if getattr(spec, "type", None) == "ExternalName" and external:
            topology.edges.append(
                DependencyEdge(
                    source_service_id=service_id,
                    target_service_id=external,
                    discovery_methods=[DiscoveryMethod.K8S_MANIFEST],
                    confidence_score=1.0,
                    last_observed_at=now,
                    provenance=MANIFEST_PROVENANCE,
                )
            )
            topology.findings.append(
                _finding(
                    namespace, f"service/{name}", ShapeFindingKind.EXTERNAL_NAME_SERVICE,
                    f"a DNS alias for {external}; calls to it are calls to that host",
                    service_id,
                )
            )
            continue
        selector = dict(getattr(spec, "selector", None) or {})
        if not selector:
            topology.findings.append(
                _finding(
                    namespace, f"service/{name}", ShapeFindingKind.SELECTORLESS_SERVICE,
                    "no selector: its backends are managed outside Kubernetes "
                    "selectors, so their environment is unknown",
                    service_id,
                )
            )
            continue
        selected = [t for t in templates if _selects(selector, t.labels)]
        if not selected:
            topology.findings.append(
                _finding(
                    namespace, f"service/{name}", ShapeFindingKind.SERVICE_SELECTS_NO_WORKLOAD,
                    "its selector matches no workload discovery read, so its "
                    "environment is unknown",
                    service_id,
                )
            )
            continue
        selected_workloads.update(t.workload for t in selected)
        topology.environments.append(_service_environment(service_id, selected, config_maps))
    _add_unexposed(topology, context, templates, selected_workloads, service_names, config_maps)
    return topology


def _add_unexposed(
    topology: NamespaceTopology,
    context: IdentityContext,
    templates: list[PodTemplate],
    selected_workloads: set[str],
    service_names: set[str],
    config_maps: Mapping[str, Mapping[str, str]],
) -> None:
    """Register the workloads no Service selects, or report why not."""
    namespace = topology.namespace
    unexposed: dict[str, list[PodTemplate]] = {}
    for template in templates:
        if template.workload not in selected_workloads:
            unexposed.setdefault(template.workload, []).append(template)
    by_name: dict[str, list[str]] = {}
    for workload, owned in sorted(unexposed.items()):
        name = workload.split("/", 1)[1]
        if not owned[0].registrable:
            topology.findings.append(
                _finding(
                    namespace, workload, ShapeFindingKind.TRANSIENT_WORKLOAD,
                    "no Service selects it and it is transient, so it is not "
                    "registered; calls it makes are not attributed",
                    None,
                )
            )
        elif name in service_names:
            topology.findings.append(
                _finding(
                    namespace, workload, ShapeFindingKind.NAME_COLLISION,
                    f"no Service selects it, but Service {name} exists and selects "
                    "other pods, so it is not registered",
                    context.identity(name, namespace).uri,
                )
            )
        else:
            by_name.setdefault(name, []).append(workload)
    for name, workloads in sorted(by_name.items()):
        service_id = context.identity(name, namespace).uri
        topology.services.append(
            ServiceNode(
                service_id=service_id,
                name=name,
                metadata={
                    "source": DiscoveryMethod.K8S_MANIFEST.value,
                    _WORKLOAD_METADATA_KEY: ",".join(workloads),
                    _EXPOSED_METADATA_KEY: "false",
                },
            )
        )
        topology.environments.append(
            _service_environment(
                service_id, [t for w in workloads for t in unexposed[w]], config_maps
            )
        )
        topology.findings.extend(
            _finding(
                namespace, workload, ShapeFindingKind.UNEXPOSED_WORKLOAD,
                "no Service selects it; registered as a service so the calls it "
                "makes count",
                service_id,
            )
            for workload in workloads
        )

"""Read-only client for extracting Kubernetes service metadata."""

import logging

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from src.core.models import DiscoveryResult, ServiceNode
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
        try:
            config.load_kube_config()
        except ConfigException:
            try:
                config.load_incluster_config()
            except ConfigException:
                logger.exception("Unable to load Kubernetes client configuration")
                return
        self._core_v1_api = client.CoreV1Api()

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

    def get_namespace_services(self, namespace: str) -> DiscoveryResult[ServiceNode]:
        """Return service nodes observed in a Kubernetes namespace.

        This method is intended to read service metadata from the specified
        namespace only. It must not create, update, or delete Kubernetes
        resources.

        Args:
            namespace: Kubernetes namespace whose services to inspect.

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
                    service_id=f"{namespace}/{metadata.name}",
                    name=metadata.name,
                )
            )
        errors = (
            [f"{namespace}: skipped {skipped} service(s) without a name"]
            if skipped
            else []
        )
        return DiscoveryResult[ServiceNode].from_items(services, errors)

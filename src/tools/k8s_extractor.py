"""Read-only client for extracting Kubernetes service metadata."""

import logging

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from src.core.models import ServiceNode


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

        Returns:
            (True, "Success") when services are listable, otherwise
            (False, <error message>).
        """
        if self._core_v1_api is None:
            return False, "Kubernetes client configuration could not be loaded"
        try:
            self._core_v1_api.list_namespaced_service(namespace, limit=1)
            return True, "Success"
        except Exception as error:
            logger.warning(
                "Kubernetes access verification failed for namespace %s: %s",
                namespace,
                error,
            )
            return False, str(error)

    def get_namespace_services(self, namespace: str) -> list[ServiceNode]:
        """Return service nodes observed in a Kubernetes namespace.

        This method is intended to read service metadata from the specified
        namespace only. It must not create, update, or delete Kubernetes
        resources.

        Args:
            namespace: Kubernetes namespace whose services to inspect.

        Returns:
            Service nodes representing services in the namespace.
        """
        if self._core_v1_api is None:
            logger.error("Kubernetes API client is unavailable")
            return []

        try:
            response = self._core_v1_api.list_namespaced_service(namespace)
            services: list[ServiceNode] = []
            for service in response.items:
                metadata = service.metadata
                if not metadata or not metadata.name:
                    logger.warning("Skipping Kubernetes service without a name")
                    continue
                services.append(
                    ServiceNode(
                        service_id=f"{namespace}/{metadata.name}",
                        name=metadata.name,
                    )
                )
            return services
        except Exception:
            logger.exception(
                "Failed to list Kubernetes services in namespace %s", namespace
            )
            return []

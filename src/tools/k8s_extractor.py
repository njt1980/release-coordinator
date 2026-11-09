"""Read-only interface for extracting Kubernetes service metadata."""

from src.core.models import ServiceNode


class KubernetesExtractor:
    """Read-only client boundary for Kubernetes namespace data."""

    def get_namespace_services(self, namespace: str) -> list[ServiceNode]:
        """Return service nodes observed in a Kubernetes namespace.

        This method is intended to read service metadata from the specified
        namespace only. It must not create, update, or delete Kubernetes
        resources.

        Args:
            namespace: Kubernetes namespace whose services to inspect.

        Returns:
            Service nodes representing services in the namespace.

        Raises:
            NotImplementedError: Until the Kubernetes client integration is added.
        """
        raise NotImplementedError("Azure SDK integration pending")

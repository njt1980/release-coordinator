"""Read-only interfaces for extracting repository and pipeline data."""


class DevOpsExtractor:
    """Read-only client boundary for DevOps pipeline and repository data."""

    def get_pipeline_variables(self, repo_name: str) -> dict[str, str]:
        """Return the configured variables for a repository's pipeline.

        This method is intended to read pipeline configuration only. It must
        not create, update, or delete pipelines, variables, or repository data.

        Args:
            repo_name: Name of the repository whose pipeline variables to read.

        Returns:
            A mapping of pipeline variable names to their string values.

        Raises:
            NotImplementedError: Until the Azure DevOps SDK integration is added.
        """
        raise NotImplementedError("Azure SDK integration pending")

    def scan_repository_for_endpoints(self, repo_name: str) -> list[str]:
        """Find endpoint references in a repository without modifying it.

        The implementation is intended to inspect repository contents and
        return discovered endpoint strings. It must not write to the
        repository or trigger builds or deployments.

        Args:
            repo_name: Name of the repository to inspect.

        Returns:
            Endpoint references discovered in the repository.

        Raises:
            NotImplementedError: Until the Azure DevOps SDK integration is added.
        """
        raise NotImplementedError("Azure SDK integration pending")

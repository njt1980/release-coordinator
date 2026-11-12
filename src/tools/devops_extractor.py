"""Read-only clients for extracting Azure DevOps repository and pipeline data."""

import ipaddress
import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

from azure.devops.connection import Connection
from azure.identity import DefaultAzureCredential
from msrest.authentication import BasicTokenAuthentication
from pydantic import BaseModel, Field

from src.tools._timeout import (
    TIMEOUT_MESSAGE,
    VERIFY_TIMEOUT_SECONDS,
    run_with_timeout,
)


logger = logging.getLogger(__name__)
_AZURE_DEVOPS_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"
_TEXT_FILE_SUFFIXES = {
    ".cfg",
    ".conf",
    ".cs",
    ".env",
    ".go",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".php",
    ".properties",
    ".py",
    ".rb",
    ".sh",
    ".toml",
    ".ts",
    ".xml",
    ".yaml",
    ".yml",
}
_ENDPOINT_PATTERN = re.compile(r"""https?://[^\s"'<>)}\]]+""", re.IGNORECASE)
_EXCLUDED_DIRECTORIES = {
    ".git",
    "bower_components",
    "build",
    "dist",
    "docs",
    "node_modules",
    "target",
    "vendor",
}
_EXCLUDED_FILENAMES = {
    "composer.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}
# Directories whose immediate children are individual microservices, e.g.
# src/order-service/... in a monorepo.
_SERVICE_CONTAINER_DIRECTORIES = {"apps", "cmd", "services", "src"}
# A single DNS label: how one in-cluster service addresses another.
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Dotted quads, including redacted placeholders such as x.x.x.x.
_IP_LIKE_PATTERN = re.compile(r"^[0-9x*]{1,3}(?:\.[0-9x*]{1,3}){3}$")
_CLUSTER_DNS_SUFFIXES = (".svc", ".svc.cluster.local")
# In-cluster names that are platform infrastructure, not application services.
_PLATFORM_SERVICE_NAMES = {"kubernetes", "localhost"}


class EndpointReference(BaseModel):
    """An endpoint URL found in a file owned by a specific microservice."""

    service_name: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    file_path: str = Field(min_length=1)


def _is_scannable_path(path: str) -> bool:
    """Return True for application or configuration source files.

    Documentation, dependency/vendor trees, build output, and lock files are
    skipped because they mostly reference third-party sites.
    """
    segments = [segment.lower() for segment in path.split("/") if segment]
    if not segments or _EXCLUDED_DIRECTORIES.intersection(segments[:-1]):
        return False
    filename = segments[-1]
    if filename in _EXCLUDED_FILENAMES:
        return False
    if filename == ".env" or filename.startswith(".env."):
        return True
    return os.path.splitext(filename)[1] in _TEXT_FILE_SUFFIXES


def _service_name_from_path(path: str) -> str | None:
    """Return the microservice that owns a repository file, if any.

    A file belongs to service ``X`` when it lives under ``<container>/X/``
    where ``<container>`` is one of ``src``, ``services``, ``apps`` or
    ``cmd`` (e.g. ``/src/order-service/routes/order.js`` -> ``order-service``).
    Files outside such a directory (CI config, charts, root manifests) have
    no owning service and return None.
    """
    segments = [segment for segment in path.split("/") if segment]
    for index, segment in enumerate(segments[:-2]):
        if segment.lower() in _SERVICE_CONTAINER_DIRECTORIES:
            candidate = segments[index + 1].lower()
            if _DNS_LABEL_PATTERN.match(candidate):
                return candidate
            return None
    return None


def _is_internal_service_host(hostname: str) -> bool:
    """Return True only for hostnames that address an in-cluster service.

    Internal hosts are a single DNS label (``order-service``) or a cluster
    DNS name (``order-service.default.svc[.cluster.local]``). Every dotted
    public domain (github.com, paypal.me, eslint.org, ...) and every IP
    address, including redacted ones like ``x.x.x.x``, is rejected.
    """
    host = hostname.lower().rstrip(".")
    if not host or _IP_LIKE_PATTERN.match(host):
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    service_label = host.split(".", 1)[0]
    if service_label in _PLATFORM_SERVICE_NAMES:
        return False
    if "." not in host:
        return bool(_DNS_LABEL_PATTERN.match(host))
    return host.endswith(_CLUSTER_DNS_SUFFIXES) and bool(
        _DNS_LABEL_PATTERN.match(service_label)
    )


def _is_noise_endpoint(endpoint: str) -> bool:
    """Return True for anything other than an internal service-to-service URL.

    Templated URLs and URLs whose host is external, local, or an IP address
    are noise.
    """
    if "${" in endpoint or "{{" in endpoint:
        return True
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname or ""
    except ValueError:
        return True
    if parsed.scheme.lower() not in {"http", "https"}:
        return True
    return not _is_internal_service_host(hostname)


class DevOpsExtractor:
    """Read-only client boundary for DevOps pipeline and repository data."""

    def __init__(self, org_url: str, project: str, repositories: list[str]) -> None:
        """Bind the extractor to an explicit Azure DevOps scope.

        Args:
            org_url: Azure DevOps organization URL, e.g. https://dev.azure.com/org.
            project: Azure DevOps project name.
            repositories: Repository names in scope for this extractor.

        Raises:
            ValueError: If org_url or project is blank.
        """
        if not org_url or not org_url.strip():
            raise ValueError("org_url is required")
        if not project or not project.strip():
            raise ValueError("project is required")
        self._credential = DefaultAzureCredential()
        self._organization_url = org_url.strip()
        self._project = project.strip()
        self._repositories = list(repositories)

    def _get_connection(self) -> Connection:
        access_token = self._credential.get_token(_AZURE_DEVOPS_SCOPE)
        credentials = BasicTokenAuthentication({"access_token": access_token.token})
        return Connection(base_url=self._organization_url, creds=credentials)

    def _get_clients(self) -> tuple[Any, Any, str]:
        connection = self._get_connection()
        return (
            connection.clients.get_build_client(),
            connection.clients.get_git_client(),
            self._project,
        )

    def verify_access(self) -> tuple[bool, str]:
        """Confirm read access by fetching the configured project's metadata.

        Token acquisition and the project lookup share a strict
        VERIFY_TIMEOUT_SECONDS deadline so a hanging credential chain or
        network call cannot block the caller.

        Returns:
            (True, "Success") when the project is readable, otherwise
            (False, <error message>).
        """

        def _check() -> None:
            connection = self._get_connection()
            connection.clients.get_core_client().get_project(self._project)

        try:
            run_with_timeout(_check, VERIFY_TIMEOUT_SECONDS)
            return True, "Success"
        except TimeoutError:
            logger.warning("Azure DevOps access verification timed out")
            return False, TIMEOUT_MESSAGE
        except Exception as error:
            logger.warning("Azure DevOps access verification failed: %s", error)
            return False, str(error)

    @staticmethod
    def _read_item_content(
        git_client: Any, repository_id: str, path: str, project: str
    ) -> str:
        content = git_client.get_item_content(repository_id, path, project=project)
        if isinstance(content, (bytes, bytearray)):
            return bytes(content).decode("utf-8", errors="replace")
        return b"".join(content).decode("utf-8", errors="replace")

    @staticmethod
    def _parse_yaml_variables(content: str) -> dict[str, str]:
        variables: dict[str, str] = {}
        lines = content.splitlines()
        section_start = next(
            (
                index
                for index, line in enumerate(lines)
                if re.match(r"^variables\s*:", line)
            ),
            None,
        )
        if section_start is None:
            return variables

        pending_name: str | None = None
        for line in lines[section_start + 1 :]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            if indent == 0:
                break

            list_entry = re.match(r"^\s*-\s*name\s*:\s*(.+?)\s*$", line)
            if list_entry:
                pending_name = list_entry.group(1).strip("\"'")
                continue

            value_entry = re.match(r"^\s+value\s*:\s*(.*?)\s*$", line)
            if pending_name and value_entry:
                value = value_entry.group(1).strip()
                variables[pending_name] = value.strip("\"'")
                pending_name = None
                continue

            mapping_entry = re.match(
                r"^\s*(?:-\s*)?([A-Za-z_][\w.-]*)\s*:\s*(.*?)\s*$", line
            )
            if mapping_entry and mapping_entry.group(1) not in {
                "name",
                "value",
                "group",
            }:
                key, value = mapping_entry.groups()
                variables[key] = value.strip().strip("\"'")
                pending_name = None

        return variables

    @staticmethod
    def _find_repository(git_client: Any, project: str, repo_name: str) -> Any:
        return next(
            (
                repository
                for repository in git_client.get_repositories(project)
                if repository.name == repo_name
            ),
            None,
        )

    def get_pipeline_variables(self, repo_name: str) -> dict[str, str]:
        """Return the configured variables for a repository's pipeline.

        This method is intended to read pipeline configuration only. It must
        not create, update, or delete pipelines, variables, or repository data.

        Args:
            repo_name: Name of the repository whose pipeline variables to read.

        Returns:
            A mapping of pipeline variable names to their string values.
        """
        try:
            build_client, git_client, project = self._get_clients()
            source_repository = self._find_repository(
                git_client, project, repo_name
            )
            if source_repository is None:
                logger.warning(
                    "Azure DevOps repository %s was not found", repo_name
                )
                return {}

            definitions = build_client.get_definitions(
                project=project,
                repository_id=source_repository.id,
                repository_type=getattr(source_repository, "type", "TfsGit"),
            )
            if not definitions:
                logger.warning(
                    "No Azure DevOps pipeline definitions found for repository %s",
                    repo_name,
                )
                return {}

            variables: dict[str, str] = {}
            for definition_summary in definitions:
                definition = build_client.get_definition(
                    definition_id=definition_summary.id, project=project
                )
                process = getattr(definition, "process", None)
                yaml_filename = getattr(process, "yaml_filename", None)
                if yaml_filename:
                    definition_repository = getattr(
                        definition, "repository", None
                    )
                    repository_id = (
                        getattr(definition_repository, "id", None)
                        or source_repository.id
                    )
                    if repository_id:
                        yaml_content = self._read_item_content(
                            git_client, repository_id, yaml_filename, project
                        )
                        variables.update(self._parse_yaml_variables(yaml_content))

                definition_variables = getattr(definition, "variables", None) or {}
                for name, variable in definition_variables.items():
                    value = getattr(variable, "value", None)
                    if value is not None and not getattr(variable, "is_secret", False):
                        variables[name] = str(value)
            return variables
        except Exception:
            logger.exception(
                "Failed to read pipeline variables for repository %s", repo_name
            )
            return {}

    def scan_repository_for_endpoints(
        self, repo_name: str
    ) -> list[EndpointReference] | None:
        """Find internal service endpoints referenced by each microservice.

        Only application and configuration files under a microservice
        directory (see ``_service_name_from_path``) are read. Every URL that
        is not an internal service-to-service call is dropped. It must not
        write to the repository or trigger builds or deployments.

        Args:
            repo_name: Name of the repository to inspect.

        Returns:
            Unique (service, endpoint) references in discovery order, or None
            if the repository could not be found or scanned.
        """
        try:
            _, git_client, project = self._get_clients()
            repository = self._find_repository(git_client, project, repo_name)
            if repository is None:
                logger.warning("Azure DevOps repository %s was not found", repo_name)
                return None

            items = git_client.get_items(
                repository.id, project=project, recursion_level="Full"
            )
            references: list[EndpointReference] = []
            seen: set[tuple[str, str]] = set()
            for item in items:
                if getattr(item, "is_folder", False):
                    continue
                path = getattr(item, "path", "")
                if not _is_scannable_path(path):
                    continue
                service_name = _service_name_from_path(path)
                if service_name is None:
                    continue
                content = self._read_item_content(
                    git_client, repository.id, path, project
                )
                for match in _ENDPOINT_PATTERN.findall(content):
                    endpoint = match.rstrip(".,;:")
                    if _is_noise_endpoint(endpoint):
                        continue
                    key = (service_name, endpoint)
                    if key not in seen:
                        seen.add(key)
                        references.append(
                            EndpointReference(
                                service_name=service_name,
                                endpoint=endpoint,
                                file_path=path,
                            )
                        )
            return references
        except Exception:
            logger.exception(
                "Failed to scan repository %s for endpoints", repo_name
            )
            return None

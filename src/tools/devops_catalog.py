"""Read-only listing of an Azure DevOps organization's projects and repositories.

Onboarding uses it so nobody types repository names: pick the project, then
the repositories (or all of them). It reads only names and metadata (Core
and Git read), with the same identity, organization allowlist and throttling
retries as the scanner (``devops_extractor``).
"""

import logging
from typing import Any, Final

from azure.identity import DefaultAzureCredential
from msrest.authentication import BasicTokenAuthentication
from pydantic import BaseModel

from src.core import config
from src.tools._timeout import run_with_timeout
from src.tools.devops_extractor import _AZURE_DEVOPS_SCOPE, Connection, configure_throttling_retries

logger = logging.getLogger(__name__)

# Listing a large organization (hundreds of repositories) takes longer than
# an access check, but must still not hang a request.
LIST_TIMEOUT_SECONDS: Final[float] = 30.0


class DevOpsProjectInfo(BaseModel):
    name: str
    description: str = ""


class DevOpsRepositoryInfo(BaseModel):
    """One repository: ``disabled`` ones can't be read; ``empty`` ones have no default branch."""

    name: str
    default_branch: str = ""
    size_bytes: int = 0
    disabled: bool = False
    empty: bool = False


class DevOpsCatalog:
    """Lists what an Azure DevOps organization holds (read-only)."""

    def __init__(self, org_url: str) -> None:
        """Bind to one organization.

        Raises:
            ValueError: if ``org_url`` is blank or not in ``RC_ADO_ORG_ALLOWLIST``
                (when one is set).
        """
        url = (org_url or "").strip()
        if not url:
            raise ValueError("org_url is required")
        if config.ADO_ORG_ALLOWLIST and url.rstrip("/").lower() not in config.ADO_ORG_ALLOWLIST:
            raise ValueError(f"{url} is not an Azure DevOps organization this server may read")
        self._organization_url = url
        self._credential = DefaultAzureCredential()

    def _connection(self) -> Connection:
        token = self._credential.get_token(_AZURE_DEVOPS_SCOPE)
        connection = Connection(
            base_url=self._organization_url, creds=BasicTokenAuthentication({"access_token": token.token})
        )
        configure_throttling_retries(connection)
        return connection

    def projects(self) -> list[DevOpsProjectInfo]:
        """Every project the identity can read, by name."""

        def fetch() -> list[Any]:
            return list(self._connection().clients.get_core_client().get_projects())

        found = run_with_timeout(fetch, LIST_TIMEOUT_SECONDS)
        return sorted(
            (DevOpsProjectInfo(name=p.name, description=getattr(p, "description", None) or "") for p in found),
            key=lambda project: project.name.lower(),
        )

    def repositories(self, project: str) -> list[DevOpsRepositoryInfo]:
        """Every repository of ``project``, by name."""
        if not project or not project.strip():
            raise ValueError("project is required")

        def fetch() -> list[Any]:
            return list(self._connection().clients.get_git_client().get_repositories(project.strip()))

        found = run_with_timeout(fetch, LIST_TIMEOUT_SECONDS)
        return sorted(
            (
                DevOpsRepositoryInfo(
                    name=repository.name,
                    default_branch=(getattr(repository, "default_branch", None) or "").removeprefix("refs/heads/"),
                    size_bytes=int(getattr(repository, "size", None) or 0),
                    disabled=bool(getattr(repository, "is_disabled", False)),
                    empty=not getattr(repository, "default_branch", None),
                )
                for repository in found
            ),
            key=lambda repository: repository.name.lower(),
        )

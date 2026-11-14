"""Run the real static scanner over a local checkout.

The scanner reads repositories through the Azure DevOps git client; here a
fake client serves files from disk, so ``DevOpsExtractor.scan_repository``
runs unchanged and offline (the same seam the unit tests use).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.models import ScannedRevision
from src.tools import devops_extractor
from src.tools.devops_extractor import DevOpsExtractor, RepositoryScan

_SKIP_DIRS = {".git"}


def list_files(root: Path) -> list[str]:
    """Every regular file under ``root`` as an ADO-style path (``/a/b``)."""
    paths: list[str] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in names:
            path = Path(current, name)
            if path.is_file() and not path.is_symlink():
                paths.append("/" + path.relative_to(root).as_posix())
    return sorted(paths)


def _fake_git_client(repo_name: str, root: Path, sha: str) -> MagicMock:
    def content(_repo_id: object, path: str, *_args: object, **_kwargs: object) -> bytes:
        return (root / path.lstrip("/")).read_bytes()

    git = MagicMock()
    git.get_repositories.return_value = [
        SimpleNamespace(name=repo_name, id="offline", default_branch="refs/heads/main")
    ]
    git.get_refs.return_value = [SimpleNamespace(name="refs/heads/main", object_id=sha)]
    git.get_items.return_value = [
        SimpleNamespace(path=path, is_folder=False) for path in list_files(root)
    ]
    git.get_item_content.side_effect = content
    return git


def scan_checkout(repo_name: str, root: Path, sha: str) -> RepositoryScan:
    """Scan ``root`` as repository ``repo_name`` at commit ``sha``.

    Raises:
        RuntimeError: the scanner did not return a repository scan.
    """
    with patch.object(devops_extractor, "DefaultAzureCredential"):
        extractor = DevOpsExtractor(
            org_url="https://dev.azure.com/offline", project="offline", repositories=[]
        )
    git = _fake_git_client(repo_name, root, sha)
    extractor._get_clients = MagicMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), git, "offline")
    )
    revision = ScannedRevision(repository=repo_name, branch="main", commit_sha=sha)
    result = extractor.scan_repository(repo_name, commit=revision)
    if not result.items:
        raise RuntimeError(f"{repo_name}: scan failed: {result.errors}")
    return result.items[0]

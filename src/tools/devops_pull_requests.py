"""Read an Azure DevOps pull request for assessment (read-only).

Pure functions over the Azure DevOps git client; ``DevOpsExtractor`` calls
them with its own read-only connection. A pull request is read at its
latest iteration: the head is that iteration's source commit and the base
its merge base with the target branch (``commonRefCommit``), so the
changed files and diff are exactly what the pull request changes, as the
Azure DevOps "Files" tab shows them.

Azure DevOps has no unified-diff API, so the diff is built here from each
changed file's text at the base and the head, within fixed limits (binary
and oversized files are listed, not diffed). Nothing is written to Azure
DevOps.
"""

import difflib
from collections.abc import Callable
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, Field

# Limits that keep a pull request's diff readable by the risk reasoner.
MAX_DIFF_FILES: Final[int] = 200
MAX_FILE_CHARS: Final[int] = 200_000
MAX_DIFF_CHARS: Final[int] = 300_000
_CHANGES_PAGE: Final[int] = 1000
_HEADS_PREFIX: Final[str] = "refs/heads/"

# Reads one file's text at one commit: (path, commit) -> text, or None when
# the file doesn't exist there.
ReadText = Callable[[str, str], str | None]
# Makes the ReadText of one repository, by repository ID.
ReaderFor = Callable[[str], ReadText]


class PullRequestError(ValueError):
    """The pull request can't be assessed (missing, abandoned, no iteration)."""


class FileChange(str, Enum):
    ADD = "add"
    EDIT = "edit"
    DELETE = "delete"
    RENAME = "rename"


class PullRequestFile(BaseModel):
    """One file the pull request changes (repository-relative, no leading slash)."""

    path: str = Field(min_length=1)
    change: FileChange
    original_path: str | None = None


class PullRequestDetails(BaseModel):
    """What the assessment needs from a pull request.

    ``skipped_files`` are changed files left out of ``diff`` (binary,
    oversized, or beyond the limits); ``diff_truncated`` says the limits
    were hit.
    """

    pr_id: int = Field(gt=0)
    repository: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    status: str = ""
    source_branch: str = ""
    target_branch: str = ""
    base_sha: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    files: list[PullRequestFile] = Field(default_factory=list)
    diff: str = ""
    diff_truncated: bool = False
    skipped_files: list[str] = Field(default_factory=list)

    @property
    def changed_paths(self) -> list[str]:
        """Every path the pull request touches (a rename's old path too)."""
        paths: list[str] = []
        for file in self.files:
            paths.append(file.path)
            if file.original_path:
                paths.append(file.original_path)
        return list(dict.fromkeys(paths))


def _get(value: Any, *names: str) -> Any:
    """An attribute of an SDK object or a key of a dict, by any of ``names``."""
    for name in names:
        found = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if found is not None:
            return found
    return None


def _commit(ref: Any) -> str:
    return str(_get(ref, "commit_id", "commitId") or "").lower()


def _branch(ref_name: str | None) -> str:
    name = ref_name or ""
    return name[len(_HEADS_PREFIX):] if name.startswith(_HEADS_PREFIX) else name


def _relative(path: str | None) -> str:
    return (path or "").lstrip("/")


def _file_change(change_type: str) -> FileChange | None:
    """Azure DevOps change types are comma-separated flags ("edit, rename")."""
    flags = {flag.strip().lower() for flag in str(change_type or "").split(",")}
    for change in (FileChange.DELETE, FileChange.RENAME, FileChange.ADD, FileChange.EDIT):
        if change.value in flags:
            return change
    return None


def changed_files(entries: list[Any]) -> list[PullRequestFile]:
    """The files among iteration change entries (folders are left out)."""
    files: list[PullRequestFile] = []
    for entry in entries:
        item = _get(entry, "item") or {}
        if _get(item, "is_folder", "isFolder") or _get(item, "git_object_type", "gitObjectType") == "tree":
            continue
        change = _file_change(_get(entry, "change_type", "changeType") or "")
        path = _relative(_get(item, "path"))
        if change is None or not path:
            continue
        original = _relative(_get(entry, "original_path", "originalPath", "source_server_item", "sourceServerItem"))
        files.append(
            PullRequestFile(
                path=path, change=change, original_path=original if original and original != path else None
            )
        )
    return files


def _text_or_none(read: ReadText, path: str, commit: str) -> tuple[str | None, bool]:
    """``(text, diffable)``: None text when absent; not diffable when binary or oversized."""
    text = read(path, commit)
    if text is None:
        return None, True
    return text, "\x00" not in text and len(text) <= MAX_FILE_CHARS


def build_diff(files: list[PullRequestFile], base: str, head: str, read: ReadText) -> tuple[str, bool, list[str]]:
    """A unified diff of ``files`` between ``base`` and ``head``.

    Returns ``(diff, truncated, skipped)``: ``skipped`` are files not
    diffed (binary, oversized, past ``MAX_DIFF_FILES`` or ``MAX_DIFF_CHARS``).
    """
    parts: list[str] = []
    size = 0
    skipped: list[str] = []
    truncated = False
    for index, file in enumerate(files):
        if index >= MAX_DIFF_FILES or size >= MAX_DIFF_CHARS:
            truncated = True
            skipped.extend(f.path for f in files[index:])
            break
        old_path = file.original_path or file.path
        old, old_ok = (None, True) if file.change is FileChange.ADD else _text_or_none(read, old_path, base)
        new, new_ok = (None, True) if file.change is FileChange.DELETE else _text_or_none(read, file.path, head)
        if not (old_ok and new_ok):
            skipped.append(file.path)
            continue
        lines = list(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                (new or "").splitlines(keepends=True),
                fromfile=f"a/{old_path}" if old is not None else "/dev/null",
                tofile=f"b/{file.path}" if new is not None else "/dev/null",
            )
        )
        chunk = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
        if not chunk and file.change is FileChange.RENAME:
            chunk = f"rename from {old_path}\nrename to {file.path}\n"
        parts.append(chunk)
        size += len(chunk)
    return "".join(parts), truncated, skipped


def _all_changes(git_client: Any, repository_id: str, pr_id: int, iteration_id: int, project: str) -> list[Any]:
    entries: list[Any] = []
    skip = 0
    while True:
        page = git_client.get_pull_request_iteration_changes(
            repository_id, pr_id, iteration_id, project=project, top=_CHANGES_PAGE, skip=skip
        )
        found = list(_get(page, "change_entries", "changeEntries") or [])
        entries.extend(found)
        next_skip = _get(page, "next_skip", "nextSkip")
        if not found or not next_skip:
            return entries
        skip = int(next_skip)


def read_pull_request(git_client: Any, project: str, pr_id: int, reader_for: ReaderFor) -> PullRequestDetails:
    """Read pull request ``pr_id`` of ``project`` at its latest iteration.

    Raises:
        PullRequestError: if it is abandoned or has no iteration.
        Exception: whatever the Azure DevOps client raises (not found,
            not authorized).
    """
    pull_request = git_client.get_pull_request_by_id(pr_id, project=project)
    status = str(_get(pull_request, "status") or "").lower()
    if status == "abandoned":
        raise PullRequestError(f"pull request {pr_id} is abandoned")
    repository = _get(pull_request, "repository") or {}
    repository_id = str(_get(repository, "id") or "")
    iterations = list(git_client.get_pull_request_iterations(repository_id, pr_id, project=project) or [])
    if not iterations:
        raise PullRequestError(f"pull request {pr_id} has no iteration to assess")
    latest = max(iterations, key=lambda iteration: int(_get(iteration, "id") or 0))
    head = _commit(_get(latest, "source_ref_commit", "sourceRefCommit")) or _commit(
        _get(pull_request, "last_merge_source_commit", "lastMergeSourceCommit")
    )
    base = _commit(_get(latest, "common_ref_commit", "commonRefCommit")) or _commit(
        _get(pull_request, "last_merge_target_commit", "lastMergeTargetCommit")
    )
    if not head or not base:
        raise PullRequestError(f"pull request {pr_id}: its commits can't be read")
    files = changed_files(_all_changes(git_client, repository_id, pr_id, int(_get(latest, "id")), project))
    diff, truncated, skipped = build_diff(files, base, head, reader_for(repository_id))
    return PullRequestDetails(
        pr_id=pr_id,
        repository=str(_get(repository, "name") or ""),
        title=str(_get(pull_request, "title") or ""),
        description=str(_get(pull_request, "description") or ""),
        status=status,
        source_branch=_branch(_get(pull_request, "source_ref_name", "sourceRefName")),
        target_branch=_branch(_get(pull_request, "target_ref_name", "targetRefName")),
        base_sha=base,
        head_sha=head,
        files=files,
        diff=diff,
        diff_truncated=truncated,
        skipped_files=skipped,
    )

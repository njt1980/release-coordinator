"""Read the names and images build files declare for a repository's services.

Identity only: a declaration ties a source directory to the name it runs
under or the image built from it, so ``identity_links`` can tie a
source-code service to its runtime counterpart. Nothing here is ever a
dependency edge (a Compose ``depends_on`` or URL is not read).

Sources, each read from the repository at the scanned commit:

- Docker Compose (``docker-compose*.yml``, ``compose*.yml``): a service
  with a ``build`` names its build directory (``COMPOSE_SERVICE``) and,
  with an ``image``, the image built there (``COMPOSE_IMAGE``).
- skaffold (``skaffold.yaml``): each build artifact's ``image`` and
  ``context`` (``SKAFFOLD_ARTIFACT``).
- Azure Pipelines YAML (``azure-pipelines*.yml`` anywhere, or any YAML
  under a pipelines directory): ``Docker@2`` tasks with a ``repository``
  and a literal ``Dockerfile`` or ``buildContext`` (``PIPELINE_DOCKER_TASK``).
  ``$(name)`` is expanded from the file's own top-level ``variables``.
- .NET Aspire AppHost projects (``*.cs`` beside the AppHost ``*.csproj``):
  ``AddProject<Projects.X>("name")``, ``AddProject("name", "path.csproj")``
  and ``AddDockerfile``/``AddNpmApp``/``AddPythonApp``-style resources with
  a directory (``ASPIRE_RESOURCE``).

Each declaration's build directory is attributed to the service whose root
is its deepest enclosing directory; one outside every service is dropped.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from enum import Enum
from typing import Any, Final

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from src.core.identity_links import image_repository
from src.core.models import IdentityDeclaration, IdentityDeclarationKind

_COMPOSE_FILE: Final[re.Pattern[str]] = re.compile(
    r"^(?:docker-)?compose(?:[.-][^/]*)?\.ya?ml$", re.IGNORECASE
)
_SKAFFOLD_FILE: Final[re.Pattern[str]] = re.compile(r"^skaffold(?:[.-][^/]*)?\.ya?ml$", re.IGNORECASE)
_PIPELINE_FILE: Final[re.Pattern[str]] = re.compile(r"^azure-pipelines[^/]*\.ya?ml$", re.IGNORECASE)
_PIPELINE_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".azure-pipelines", ".azuredevops", ".pipelines", "azure-pipelines", "pipelines"}
)
_YAML_SUFFIXES: Final[tuple[str, ...]] = (".yml", ".yaml")
# Directories never holding a declaration this module trusts.
_SKIPPED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".git", "bin", "node_modules", "obj", "vendor"}
)
_PIPELINE_ROOT_PREFIXES: Final[tuple[str, ...]] = (
    "$(build.sourcesdirectory)/",
    "$(system.defaultworkingdirectory)/",
    "$(pipeline.workspace)/s/",
)
_PIPELINE_VARIABLE: Final[re.Pattern[str]] = re.compile(r"\$\(([A-Za-z0-9_.-]+)\)")
_ASPIRE_TYPED_PROJECT: Final[re.Pattern[str]] = re.compile(
    r"\bAddProject\s*<\s*(?:global::)?Projects\.([A-Za-z_][A-Za-z0-9_]*)\s*>\s*\(\s*\"([^\"]+)\""
)
_ASPIRE_PATH_PROJECT: Final[re.Pattern[str]] = re.compile(
    r"\bAddProject\s*\(\s*\"([^\"]+)\"\s*,\s*@?\"([^\"]+\.csproj)\""
)
_ASPIRE_DIRECTORY_RESOURCE: Final[re.Pattern[str]] = re.compile(
    r"\bAdd(?:Dockerfile|NpmApp|JavaScriptApp|ViteApp|PythonApp|UvicornApp)\s*\(\s*\"([^\"]+)\"\s*,\s*@?\"([^\"]+)\""
)
_LINE_COMMENT: Final[re.Pattern[str]] = re.compile(r"^\s*//.*$", re.MULTILINE)


class DeclarationFileKind(str, Enum):
    """Which reader a repository file needs."""

    COMPOSE = "COMPOSE"
    SKAFFOLD = "SKAFFOLD"
    PIPELINE = "PIPELINE"
    ASPIRE = "ASPIRE"


class BuildDeclaration(IdentityDeclaration, frozen=True):
    """A declaration and the repository-relative directory it was built from."""

    build_directory: str = Field(description="Repository-relative, no slashes at the ends.")

    def declaration(self) -> IdentityDeclaration:
        return IdentityDeclaration(kind=self.kind, value=self.value, file_path=self.file_path)


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _relative(path: str) -> str:
    return "/".join(_segments(path))


def _directory(path: str) -> str:
    return "/".join(_segments(path)[:-1])


def _resolve(base: str, relative: str) -> str | None:
    """``relative`` resolved against repository directory ``base``; None if
    it leaves the repository or isn't literal."""
    value = relative.strip().replace("\\", "/")
    if not value or "$" in value or "*" in value:
        return None
    joined = posixpath.normpath(posixpath.join(base or ".", value.lstrip("/")))
    if joined == ".." or joined.startswith("../"):
        return None
    return "" if joined == "." else _relative(joined)


def declaration_file_kind(path: str, apphost_roots: Collection[str] = ()) -> DeclarationFileKind | None:
    """The reader ``path`` needs, or None when it declares nothing.

    ``apphost_roots`` are the repository-relative directories of .NET
    Aspire AppHost projects, whose ``*.cs`` files are read.
    """
    segments = _segments(path)
    if not segments or any(s.lower() in _SKIPPED_DIRECTORIES for s in segments[:-1]):
        return None
    filename = segments[-1]
    if _COMPOSE_FILE.match(filename):
        return DeclarationFileKind.COMPOSE
    if _SKAFFOLD_FILE.match(filename):
        return DeclarationFileKind.SKAFFOLD
    if _PIPELINE_FILE.match(filename) or (
        filename.lower().endswith(_YAML_SUFFIXES)
        and any(s.lower() in _PIPELINE_DIRECTORIES for s in segments[:-1])
    ):
        return DeclarationFileKind.PIPELINE
    if filename.lower().endswith(".cs") and "/".join(segments[:-1]) in apphost_roots:
        return DeclarationFileKind.ASPIRE
    return None


def _yaml_documents(content: str) -> list[Any]:
    try:
        return [doc for doc in yaml.safe_load_all(content) if doc is not None]
    except yaml.YAMLError:
        return []


def _compose(path: str, content: str) -> Iterator[BuildDeclaration]:
    base = _directory(path)
    for document in _yaml_documents(content):
        services = document.get("services") if isinstance(document, Mapping) else None
        if not isinstance(services, Mapping):
            continue
        for name, service in services.items():
            if not isinstance(service, Mapping) or not str(name).strip():
                continue
            build = service.get("build")
            if isinstance(build, str):
                directory = _resolve(base, build)
            elif isinstance(build, Mapping):
                context = _resolve(base, str(build.get("context") or "."))
                dockerfile = build.get("dockerfile")
                directory = (
                    _resolve(context, _directory(str(dockerfile)) or ".")
                    if context is not None and isinstance(dockerfile, str)
                    else context
                )
            else:
                continue  # an image only: built elsewhere, nothing to attribute
            if directory is None:
                continue
            yield BuildDeclaration(
                kind=IdentityDeclarationKind.COMPOSE_SERVICE,
                value=str(name).strip(),
                file_path=_relative(path),
                build_directory=directory,
            )
            repository = image_repository(str(service.get("image") or ""))
            if repository:
                yield BuildDeclaration(
                    kind=IdentityDeclarationKind.COMPOSE_IMAGE,
                    value=repository,
                    file_path=_relative(path),
                    build_directory=directory,
                )


def _skaffold_artifacts(document: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    sections = [document.get("build")]
    for profile in document.get("profiles") or []:
        if isinstance(profile, Mapping):
            sections.append(profile.get("build"))
    for build in sections:
        if isinstance(build, Mapping):
            for artifact in build.get("artifacts") or []:
                if isinstance(artifact, Mapping):
                    yield artifact


def _skaffold(path: str, content: str) -> Iterator[BuildDeclaration]:
    base = _directory(path)
    for document in _yaml_documents(content):
        if not isinstance(document, Mapping):
            continue
        for artifact in _skaffold_artifacts(document):
            repository = image_repository(str(artifact.get("image") or ""))
            context = _resolve(base, str(artifact.get("context") or "."))
            docker = artifact.get("docker")
            dockerfile = docker.get("dockerfile") if isinstance(docker, Mapping) else None
            directory = (
                _resolve(context, _directory(str(dockerfile)) or ".")
                if context is not None and isinstance(dockerfile, str)
                else context
            )
            if repository and directory is not None:
                yield BuildDeclaration(
                    kind=IdentityDeclarationKind.SKAFFOLD_ARTIFACT,
                    value=repository,
                    file_path=_relative(path),
                    build_directory=directory,
                )


def _pipeline_variables(document: Mapping[str, Any]) -> dict[str, str]:
    """The file's own top-level literal variables (mapping or list form)."""
    raw = document.get("variables")
    found: dict[str, str] = {}
    if isinstance(raw, Mapping):
        found = {str(k): str(v) for k, v in raw.items() if isinstance(v, (str, int, float))}
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping) and "name" in entry and isinstance(entry.get("value"), (str, int, float)):
                found[str(entry["name"])] = str(entry["value"])
    return found


def _expand(value: str, variables: Mapping[str, str]) -> str:
    return _PIPELINE_VARIABLE.sub(lambda m: variables.get(m.group(1), m.group(0)), value)


def _docker_tasks(node: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(node, Mapping):
        task = node.get("task")
        if isinstance(task, str) and task.strip().lower().startswith("docker@2"):
            inputs = node.get("inputs")
            if isinstance(inputs, Mapping):
                yield inputs
        for value in node.values():
            yield from _docker_tasks(value)
    elif isinstance(node, list):
        for value in node:
            yield from _docker_tasks(value)


def _pipeline_path(value: str) -> str:
    lowered = value.strip().lower()
    for prefix in _PIPELINE_ROOT_PREFIXES:
        if lowered.startswith(prefix):
            return value.strip()[len(prefix):]
    return value.strip()


def _pipeline(path: str, content: str) -> Iterator[BuildDeclaration]:
    for document in _yaml_documents(content):
        if not isinstance(document, Mapping):
            continue
        variables = _pipeline_variables(document)
        for inputs in _docker_tasks(document):
            repository = image_repository(_expand(str(inputs.get("repository") or ""), variables))
            dockerfile = inputs.get("Dockerfile") or inputs.get("dockerfile")
            context = inputs.get("buildContext") or inputs.get("buildcontext")
            directory = None
            if isinstance(dockerfile, str):
                directory = _resolve("", _directory(_pipeline_path(_expand(dockerfile, variables))) or ".")
            if directory is None and isinstance(context, str):
                directory = _resolve("", _pipeline_path(_expand(context, variables)) or ".")
            if repository and directory is not None:
                yield BuildDeclaration(
                    kind=IdentityDeclarationKind.PIPELINE_DOCKER_TASK,
                    value=repository,
                    file_path=_relative(path),
                    build_directory=directory,
                )


def _aspire_project_directory(identifier: str, project_files: Iterable[str]) -> str | None:
    """The directory of the one ``*.csproj`` Aspire names ``Projects.<identifier>``
    (file stem with ``.`` and ``-`` replaced by ``_``); None if not unique."""
    matches = {
        _directory(path)
        for path in project_files
        if path.lower().endswith(".csproj")
        and re.sub(r"[.\-]", "_", posixpath.basename(path)[: -len(".csproj")]) == identifier
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _aspire(path: str, content: str, file_paths: Collection[str]) -> Iterator[BuildDeclaration]:
    base = _directory(path)
    code = _LINE_COMMENT.sub("", content)
    found: list[tuple[str, str | None]] = []
    for identifier, name in _ASPIRE_TYPED_PROJECT.findall(code):
        found.append((name, _aspire_project_directory(identifier, file_paths)))
    for name, project in _ASPIRE_PATH_PROJECT.findall(code):
        resolved = _resolve(base, project)
        found.append((name, _directory(resolved) if resolved is not None else None))
    for name, directory in _ASPIRE_DIRECTORY_RESOURCE.findall(code):
        found.append((name, _resolve(base, directory)))
    for name, directory in found:
        if directory is not None and name.strip():
            yield BuildDeclaration(
                kind=IdentityDeclarationKind.ASPIRE_RESOURCE,
                value=name.strip(),
                file_path=_relative(path),
                build_directory=directory,
            )


def read_declarations(
    path: str, content: str, kind: DeclarationFileKind, file_paths: Collection[str] = ()
) -> list[BuildDeclaration]:
    """Every declaration in one file; an unparsable file declares nothing.

    ``file_paths`` (the repository's files) resolve Aspire's
    ``Projects.<Name>`` to a project file.
    """
    if kind is DeclarationFileKind.COMPOSE:
        found = list(_compose(path, content))
    elif kind is DeclarationFileKind.SKAFFOLD:
        found = list(_skaffold(path, content))
    elif kind is DeclarationFileKind.PIPELINE:
        found = list(_pipeline(path, content))
    else:
        found = list(_aspire(path, content, [_relative(p) for p in file_paths]))
    return list(dict.fromkeys(found))


def attribute_declarations(
    declarations: Iterable[BuildDeclaration],
    service_roots: Collection[str],
    tooling_directories: Collection[str] = (),
) -> dict[str, list[IdentityDeclaration]]:
    """Service root -> the declarations whose build directory it encloses
    most deeply (sorted, unique).

    Declarations outside every root are dropped, as are those built from a
    tooling directory (``tooling_directories``, lower-case names such as
    ``docker``) below the root: an image built there (a database for local
    runs) is not the service's own.
    """
    attributed: dict[str, set[IdentityDeclaration]] = {}
    for declaration in declarations:
        directory = declaration.build_directory
        owners = [
            root for root in service_roots
            if root == "" or directory == root or directory.startswith(root + "/")
        ]
        if not owners:
            continue
        owner = max(owners, key=len)
        below = _segments(directory[len(owner):])
        if any(segment.lower() in tooling_directories for segment in below):
            continue
        attributed.setdefault(owner, set()).add(declaration.declaration())
    return {
        root: sorted(found, key=lambda d: (d.kind.value, d.value, d.file_path))
        for root, found in attributed.items()
    }

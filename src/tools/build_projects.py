"""Read build files to find service projects, shared libraries and tests.

Pure functions the repository scanner calls before attributing files.

Understood build files:

- .NET ``*.csproj``: a project is an Aspire AppHost (``Aspire.AppHost.Sdk``
  or ``IsAspireHost``), a test (``IsTestProject``, a test SDK such as
  ``MSTest.Sdk``, or a test framework package), a client app (``UseMaui``,
  ``UseWPF``, ``UseWindowsForms``: it runs on devices, not in the cluster),
  runnable (``Microsoft.NET.Sdk.Web``, ``Microsoft.NET.Sdk.Worker``, or
  ``OutputType`` ``Exe``/``WinExe``) or a library; ``<ProjectReference>``
  paths are resolved relative to the file. ``<Protobuf Include=...
  GrpcServices=...>`` items are the project's contract files
  (``ContractLink``): Client, Server or Both (the default); ``None``
  (messages only) is not recorded. ``<OpenApiReference Include=...>``
  items (NSwag client generation) are OpenAPI contracts it calls (Client).
- Maven ``pom.xml``: ``packaging`` ``pom`` is an aggregator; a
  ``spring-boot-maven-plugin`` under ``build/plugins`` is runnable, whether
  the module declares it or inherits it from a parent POM in the
  repository, directly or in a profile that is active for the module
  (``activeByDefault``, or ``activation/file/exists`` naming a file the
  module has); anything else is a library. A ``<dependency>`` whose
  ``artifactId`` is another module's in the repository is a project
  reference.
- Gradle ``build.gradle``/``build.gradle.kts``: the ``org.springframework.boot``
  or ``application`` plugin is runnable; a directory whose
  ``settings.gradle`` includes subprojects is an aggregator. ``project(':a:b')``
  resolves against the nearest enclosing settings file.

``RepositoryLayout`` then decides which services own each file: a library's
files belong to every service that consumes it, directly or through other
libraries; test projects and unconsumed libraries belong to no service;
AppHost projects (local orchestration, like Docker Compose) are excluded.
"""

import fnmatch
import posixpath
import re
import xml.etree.ElementTree as ElementTree
from enum import Enum
from typing import Final

from pydantic import BaseModel, Field

from src.core.models import (
    ContractLink,
    ContractRole,
    OpaqueReferenceKind,
    OpaqueReferenceSite,
    SharedLibrary,
)


class BuildSystem(str, Enum):
    """The build tool a project file belongs to."""

    DOTNET = "DOTNET"
    MAVEN = "MAVEN"
    GRADLE = "GRADLE"


class ProjectKind(str, Enum):
    """What a build project produces.

    SERVICE: something that runs on its own (a web app, worker, Spring
        Boot application or console executable).
    LIBRARY: code that ships inside the projects referencing it.
    TEST: a test project; never deployed.
    CLIENT: a mobile or desktop app; it runs on devices, not in the cluster.
    APPHOST: a .NET Aspire AppHost, which orchestrates other projects locally.
    AGGREGATOR: a Maven parent or Gradle root that only groups modules.
    """

    SERVICE = "SERVICE"
    LIBRARY = "LIBRARY"
    TEST = "TEST"
    CLIENT = "CLIENT"
    APPHOST = "APPHOST"
    AGGREGATOR = "AGGREGATOR"


class BuildProject(BaseModel):
    """One project a build file declares.

    ``root`` is the repository-relative directory of ``file_path`` (no
    leading slash). ``references`` are the roots of the repository projects
    it references; ``unresolved`` are references that don't resolve.
    ``contracts`` are the contract files it compiles.
    """

    root: str
    file_path: str = Field(min_length=1)
    system: BuildSystem
    kind: ProjectKind
    artifact_id: str | None = None
    references: list[str] = Field(default_factory=list)
    unresolved: list[OpaqueReferenceSite] = Field(default_factory=list)
    contracts: list[ContractLink] = Field(default_factory=list)


class BuildParseError(ValueError):
    """A build file that can't be parsed."""


_MAX_BUILD_FILE_BYTES: Final[int] = 1_000_000
_GRADLE_BUILD_FILES: Final[frozenset[str]] = frozenset({"build.gradle", "build.gradle.kts"})
_GRADLE_SETTINGS_FILES: Final[frozenset[str]] = frozenset(
    {"settings.gradle", "settings.gradle.kts"}
)
_DOTNET_SERVICE_SDKS: Final[tuple[str, ...]] = ("microsoft.net.sdk.web", "microsoft.net.sdk.worker")
# Test SDKs, and packages only test projects reference.
_DOTNET_TEST_SDKS: Final[tuple[str, ...]] = ("mstest.sdk", "microsoft.net.test.sdk")
_DOTNET_TEST_PACKAGES: Final[frozenset[str]] = frozenset(
    {"microsoft.net.test.sdk", "mstest", "mstest.testframework", "xunit", "xunit.v3", "nunit", "tunit"}
)
# Properties that make a project a device (mobile/desktop) application.
_DOTNET_CLIENT_PROPERTIES: Final[tuple[str, ...]] = ("usemaui", "usewpf", "usewindowsforms")
_DOTNET_APPHOST_SDK: Final[str] = "aspire.apphost.sdk"
_SPRING_BOOT_MAVEN_PLUGIN: Final[str] = "spring-boot-maven-plugin"
_GRADLE_SERVICE_PLUGIN: Final[re.Pattern[str]] = re.compile(
    r"""(?:\bid\s*\(?\s*['"](?:org\.springframework\.boot|application)['"]"""
    r"""|\bapply\s*\(?\s*plugin\s*[:=]\s*['"](?:org\.springframework\.boot|application)['"]"""
    r"""|^\s*application\b)""",
    re.MULTILINE,
)
_GRADLE_PROJECT_REFERENCE: Final[re.Pattern[str]] = re.compile(
    r"""\bproject\(\s*(?:path\s*[:=]\s*)?['"](?P<path>:[^'"]*)['"]"""
)
# Type-safe project accessors (``projects.libs.core``) aren't resolved.
_GRADLE_ACCESSOR: Final[re.Pattern[str]] = re.compile(r"\bprojects\.[A-Za-z]")
_GRADLE_INCLUDE: Final[re.Pattern[str]] = re.compile(r"^\s*include\b", re.MULTILINE)


def build_file_system(path: str) -> BuildSystem | None:
    """Return the build system ``path`` declares a project for, or None."""
    filename = posixpath.basename(path).lower()
    if filename.endswith(".csproj"):
        return BuildSystem.DOTNET
    if filename == "pom.xml":
        return BuildSystem.MAVEN
    if filename in _GRADLE_BUILD_FILES:
        return BuildSystem.GRADLE
    return None


def is_gradle_settings(path: str) -> bool:
    """Whether ``path`` is a Gradle settings file."""
    return posixpath.basename(path).lower() in _GRADLE_SETTINGS_FILES


def _relative(path: str) -> str:
    return path.strip("/")


def _directory(path: str) -> str:
    return posixpath.dirname(_relative(path))


def _line_of(content: str, needle: str) -> int:
    position = content.find(needle)
    return content.count("\n", 0, position) + 1 if position >= 0 else 1


def _xml_root(content: str, path: str) -> ElementTree.Element:
    if len(content) > _MAX_BUILD_FILE_BYTES:
        raise BuildParseError(f"{path}: larger than {_MAX_BUILD_FILE_BYTES} bytes")
    try:
        root = ElementTree.fromstring(content.lstrip("﻿"))
    except ElementTree.ParseError as error:
        raise BuildParseError(f"{path}: {error}") from error
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def _text(element: ElementTree.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


# --- .NET ------------------------------------------------------------------


_GRPC_ROLES: Final[dict[str, ContractRole]] = {
    "client": ContractRole.CLIENT,
    "server": ContractRole.SERVER,
    "both": ContractRole.BOTH,
}


def _resolve_includes(include_attribute: str, directory: str, file_paths: set[str]) -> list[str]:
    """Repository paths a project item's ``Include`` names (globs allowed)."""
    matches: list[str] = []
    for include in include_attribute.split(";"):
        include = include.strip().replace("\\", "/")
        if not include or "$(" in include or "://" in include:
            continue
        target = posixpath.normpath(posixpath.join(directory, include))
        if any(c in target for c in "*?["):
            matches.extend(sorted(p for p in file_paths if fnmatch.fnmatch(p, target)))
        elif target in file_paths:
            matches.append(target)
    return matches


def _openapi_contracts(root: ElementTree.Element, path: str, file_paths: set[str]) -> list[ContractLink]:
    """The ``<OpenApiReference>`` items of a csproj: OpenAPI documents the
    project generates a client for."""
    directory = _directory(path)
    links: dict[str, ContractLink] = {}
    for element in root.iter("OpenApiReference"):
        for match in _resolve_includes(element.get("Include", ""), directory, file_paths):
            links[match] = ContractLink(path=match, role=ContractRole.CLIENT, project_file=_relative(path))
    return [links[key] for key in sorted(links)]


def _protobuf_contracts(root: ElementTree.Element, path: str, file_paths: set[str]) -> list[ContractLink]:
    """The ``<Protobuf>`` items of a csproj, resolved to repository paths."""
    directory = _directory(path)
    links: dict[str, ContractLink] = {}
    for element in root.iter("Protobuf"):
        services = element.get("GrpcServices")
        if services is None:
            child = element.find("GrpcServices")
            services = _text(child) if child is not None else "Both"
        role = _GRPC_ROLES.get(services.strip().lower())
        if role is None:
            continue
        for include in element.get("Include", "").split(";"):
            include = include.strip().replace("\\", "/")
            if not include or "$(" in include:
                continue
            target = posixpath.normpath(posixpath.join(directory, include))
            matches = (
                sorted(p for p in file_paths if fnmatch.fnmatch(p, target))
                if any(c in target for c in "*?[")
                else [target] if target in file_paths else []
            )
            for match in matches:
                links[match] = ContractLink(path=match, role=role, project_file=_relative(path))
    return [links[key] for key in sorted(links)]


def parse_csproj(content: str, path: str, file_paths: set[str]) -> BuildProject:
    """Read a ``*.csproj``; ``file_paths`` are the repository's files.

    Raises:
        BuildParseError: if the file isn't XML.
    """
    root = _xml_root(content, path)
    sdks = [root.get("Sdk", "")]
    sdks += [element.get("Name", "") for element in root.iter("Sdk")]
    sdks += [element.get("Sdk", "") for element in root.iter("Import")]
    sdk_names = {name.strip().lower() for sdk in sdks for name in sdk.split(";") if name.strip()}
    properties = {
        child.tag.lower(): _text(child)
        for group in root.iter("PropertyGroup")
        for child in group
        if isinstance(child.tag, str)
    }
    packages = {element.get("Include", "").strip().lower() for element in root.iter("PackageReference")}
    if any(name.startswith(_DOTNET_APPHOST_SDK) for name in sdk_names) or (
        properties.get("isaspirehost", "").lower() == "true"
    ):
        kind = ProjectKind.APPHOST
    elif (
        properties.get("istestproject", "").lower() == "true"
        or any(name.startswith(_DOTNET_TEST_SDKS) for name in sdk_names)
        or packages & _DOTNET_TEST_PACKAGES
    ):
        kind = ProjectKind.TEST
    elif any(properties.get(name, "").lower() == "true" for name in _DOTNET_CLIENT_PROPERTIES):
        kind = ProjectKind.CLIENT
    elif any(name.startswith(_DOTNET_SERVICE_SDKS) for name in sdk_names) or (
        properties.get("outputtype", "").lower() in ("exe", "winexe")
    ):
        kind = ProjectKind.SERVICE
    else:
        kind = ProjectKind.LIBRARY
    references: list[str] = []
    unresolved: list[OpaqueReferenceSite] = []
    directory = _directory(path)
    for element in root.iter("ProjectReference"):
        include = element.get("Include", "").strip()
        if not include:
            continue
        target = posixpath.normpath(posixpath.join(directory, include.replace("\\", "/")))
        if "$(" in include or target.startswith("..") or target not in file_paths:
            unresolved.append(
                OpaqueReferenceSite(
                    line=_line_of(content, include),
                    kind=OpaqueReferenceKind.PROJECT_REFERENCE_UNRESOLVED,
                    detail=include,
                )
            )
            continue
        references.append(posixpath.dirname(target))
    return BuildProject(
        root=directory,
        file_path=_relative(path),
        system=BuildSystem.DOTNET,
        kind=kind,
        references=list(dict.fromkeys(references)),
        unresolved=unresolved,
        contracts=[*_protobuf_contracts(root, path, file_paths), *_openapi_contracts(root, path, file_paths)],
    )


# --- Maven -----------------------------------------------------------------


class MavenModule(BaseModel):
    """What a ``pom.xml`` says, before modules are related to each other.

    ``boot_plugin`` is when the Spring Boot plugin applies to a module
    inheriting this POM: ``always`` (``build/plugins``), ``default`` (a
    profile active by default) or ``file:<path>`` (a profile activated when
    the module has that file). ``parent`` is the parent's artifactId.
    """

    project: BuildProject
    packaging: str = ""
    parent: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    boot_plugin: list[str] = Field(default_factory=list)


def _has_boot_plugin(build: ElementTree.Element | None) -> bool:
    return build is not None and any(
        _text(plugin.find("artifactId")) == _SPRING_BOOT_MAVEN_PLUGIN
        for plugin in build.findall("./plugins/plugin")
    )


def _boot_plugin_conditions(root: ElementTree.Element) -> list[str]:
    conditions = ["always"] if _has_boot_plugin(root.find("build")) else []
    for profile in root.findall("./profiles/profile"):
        if not _has_boot_plugin(profile.find("build")):
            continue
        activation = profile.find("activation")
        if activation is not None and _text(activation.find("activeByDefault")).lower() == "true":
            conditions.append("default")
        exists = _text(activation.find("./file/exists")) if activation is not None else ""
        if exists:
            exists = re.sub(r"^\$\{(?:project\.)?basedir\}/", "", exists)
            conditions.append(f"file:{exists}")
    return conditions


def parse_pom(content: str, path: str) -> MavenModule:
    """Read a ``pom.xml``; its kind and references are settled by
    ``resolve_maven_modules`` once every module is known.

    Raises:
        BuildParseError: if the file isn't XML.
    """
    root = _xml_root(content, path)
    dependencies = [
        _text(dependency.find("artifactId"))
        for dependency in root.findall("./dependencies/dependency")
    ]
    return MavenModule(
        project=BuildProject(
            root=_directory(path),
            file_path=_relative(path),
            system=BuildSystem.MAVEN,
            kind=ProjectKind.LIBRARY,
            artifact_id=_text(root.find("artifactId")) or None,
        ),
        packaging=_text(root.find("packaging")).lower(),
        parent=_text(root.find("./parent/artifactId")) or None,
        dependencies=[name for name in dependencies if name],
        boot_plugin=_boot_plugin_conditions(root),
    )


def _boot_plugin_applies(conditions: list[str], module_root: str, file_paths: set[str]) -> bool:
    for condition in conditions:
        if condition in ("always", "default"):
            return True
        if condition.startswith("file:"):
            target = posixpath.normpath(posixpath.join(module_root, condition[len("file:") :]))
            if target in file_paths:
                return True
    return False


def resolve_maven_modules(modules: list[MavenModule], file_paths: set[str]) -> list[BuildProject]:
    """Settle each module's kind and its references to sibling modules.

    Packaging ``pom`` is an aggregator. A module is a service when the
    Spring Boot plugin applies to it from its own POM or any ancestor in
    the repository (see ``MavenModule.boot_plugin``). A dependency on an
    artifactId exactly one module declares is a project reference; any
    other dependency is external.
    """
    owners: dict[str, list[MavenModule]] = {}
    for module in modules:
        if module.project.artifact_id:
            owners.setdefault(module.project.artifact_id, []).append(module)
    resolved: list[BuildProject] = []
    for module in modules:
        conditions = list(module.boot_plugin)
        seen: set[str] = set()
        parent = module.parent
        while parent and parent not in seen and len(owners.get(parent, [])) == 1:
            seen.add(parent)
            ancestor = owners[parent][0]
            conditions.extend(ancestor.boot_plugin)
            parent = ancestor.parent
        if module.packaging == "pom":
            kind = ProjectKind.AGGREGATOR
        elif _boot_plugin_applies(conditions, module.project.root, file_paths):
            kind = ProjectKind.SERVICE
        else:
            kind = ProjectKind.LIBRARY
        references = [
            owners[name][0].project.root
            for name in module.dependencies
            if len(owners.get(name, [])) == 1 and owners[name][0].project.root != module.project.root
        ]
        resolved.append(
            module.project.model_copy(update={"kind": kind, "references": list(dict.fromkeys(references))})
        )
    return resolved


# --- Gradle ----------------------------------------------------------------


def _settings_root(directory: str, settings_directories: set[str]) -> str:
    """The nearest directory at or above ``directory`` holding settings.gradle."""
    current = directory
    while True:
        if current in settings_directories:
            return current
        if not current:
            return ""
        current = posixpath.dirname(current)


def parse_gradle(
    content: str,
    path: str,
    settings: dict[str, str],
    directories: set[str],
) -> BuildProject:
    """Read a ``build.gradle(.kts)``.

    ``settings`` maps each settings file's directory to its content;
    ``directories`` are every directory holding a repository file.
    """
    directory = _directory(path)
    settings_text = settings.get(directory)
    if settings_text is not None and _GRADLE_INCLUDE.search(settings_text):
        kind = ProjectKind.AGGREGATOR
    elif _GRADLE_SERVICE_PLUGIN.search(content):
        kind = ProjectKind.SERVICE
    else:
        kind = ProjectKind.LIBRARY
    base = _settings_root(directory, set(settings))
    references: list[str] = []
    unresolved: list[OpaqueReferenceSite] = []
    for match in _GRADLE_PROJECT_REFERENCE.finditer(content):
        segments = [s for s in match.group("path").split(":") if s]
        target = posixpath.join(base, *segments) if segments else base
        if target == directory:
            continue
        if target in directories:
            references.append(target)
        else:
            unresolved.append(
                OpaqueReferenceSite(
                    line=content.count("\n", 0, match.start()) + 1,
                    kind=OpaqueReferenceKind.PROJECT_REFERENCE_UNRESOLVED,
                    detail=match.group("path"),
                )
            )
    for match in _GRADLE_ACCESSOR.finditer(content):
        unresolved.append(
            OpaqueReferenceSite(
                line=content.count("\n", 0, match.start()) + 1,
                kind=OpaqueReferenceKind.PROJECT_REFERENCE_UNRESOLVED,
                detail="type-safe project accessor",
            )
        )
    return BuildProject(
        root=directory,
        file_path=_relative(path),
        system=BuildSystem.GRADLE,
        kind=kind,
        references=list(dict.fromkeys(references)),
        unresolved=unresolved,
    )


# --- Repository layout -----------------------------------------------------


def directories_of(file_paths: list[str]) -> set[str]:
    """Every directory (and ancestor) holding a file, relative, root ``""``."""
    found: set[str] = {""}
    for path in file_paths:
        directory = _directory(path)
        while directory and directory not in found:
            found.add(directory)
            directory = posixpath.dirname(directory)
    return found


def read_projects(
    build_files: dict[str, str],
    file_paths: list[str],
) -> tuple[list[BuildProject], dict[str, str]]:
    """Parse every build file; return the projects and the files that failed.

    ``build_files`` maps paths (as listed, leading slash allowed) to their
    content: project files and Gradle settings files. Failures map each
    path to the error.
    """
    relative_files = {_relative(path) for path in file_paths}
    directories = directories_of(file_paths)
    settings = {
        _directory(path): content
        for path, content in build_files.items()
        if is_gradle_settings(path)
    }
    projects: list[BuildProject] = []
    maven: list[MavenModule] = []
    failures: dict[str, str] = {}
    for path, content in build_files.items():
        system = build_file_system(path)
        try:
            if system is BuildSystem.DOTNET:
                projects.append(parse_csproj(content, path, relative_files))
            elif system is BuildSystem.MAVEN:
                maven.append(parse_pom(content, path))
            elif system is BuildSystem.GRADLE:
                projects.append(parse_gradle(content, path, settings, directories))
        except BuildParseError as error:
            failures[_relative(path)] = str(error)
    projects.extend(resolve_maven_modules(maven, relative_files))
    return projects, failures


class Ownership(BaseModel):
    """Who owns one repository file.

    ``services`` are the roots of the services whose code it is (several
    for a shared library's file); ``library`` the library root when it
    belongs to one; ``excluded`` is True for local orchestration (AppHost)
    projects, which no service owns and which aren't reported.
    """

    services: list[str] = Field(default_factory=list)
    library: str | None = None
    excluded: bool = False


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _contains(root: str, path: str) -> bool:
    root_segments = _segments(root)
    return _segments(path)[: len(root_segments)] == root_segments


_CARVED_KINDS: Final[frozenset[ProjectKind]] = frozenset(
    {ProjectKind.LIBRARY, ProjectKind.TEST, ProjectKind.CLIENT, ProjectKind.APPHOST}
)


class RepositoryLayout(BaseModel):
    """Services, libraries and carved-out projects of one repository.

    ``service_roots`` are the service boundaries. ``carved`` maps the root
    of every library, test, client or AppHost project that isn't itself a service
    root to its kind: its files never belong to an enclosing service.
    ``consumers`` maps each library root to the services consuming it.
    """

    service_roots: list[str] = Field(default_factory=list)
    carved: dict[str, ProjectKind] = Field(default_factory=dict)
    consumers: dict[str, list[str]] = Field(default_factory=dict)
    project_files: dict[str, str] = Field(default_factory=dict)

    def _deepest(self, path: str) -> tuple[str, ProjectKind | None] | None:
        """The deepest service root (kind None) or carved root containing ``path``."""
        best: tuple[str, ProjectKind | None] | None = None
        depth = -1
        candidates: list[tuple[str, ProjectKind | None]] = [
            *((root, None) for root in self.service_roots),
            *self.carved.items(),
        ]
        for root, kind in candidates:
            root_depth = len(_segments(root))
            if root_depth > depth and _contains(root, path):
                best, depth = (root, kind), root_depth
        return best

    def owner(self, path: str) -> Ownership:
        """Return who owns the file at ``path``."""
        found = self._deepest(_relative(path))
        if found is None:
            return Ownership()
        root, kind = found
        if kind is None:
            return Ownership(services=[root])
        if kind is ProjectKind.APPHOST:
            return Ownership(excluded=True)
        if kind is ProjectKind.LIBRARY and self.consumers.get(root):
            return Ownership(services=list(self.consumers[root]), library=root)
        return Ownership()

    def libraries_of(self, service_root: str) -> list[str]:
        """The library roots ``service_root`` consumes, sorted."""
        return sorted(root for root, users in self.consumers.items() if service_root in users)

    def shared_libraries(self) -> list[SharedLibrary]:
        """The consumed libraries, as a coverage record stores them."""
        return [
            SharedLibrary(
                root=root,
                project_file=self.project_files.get(root, root or "."),
                consumers=sorted(users),
            )
            for root, users in sorted(self.consumers.items())
            if users
        ]


def build_layout(projects: list[BuildProject], service_roots: list[str]) -> RepositoryLayout:
    """Decide which services own which projects' files.

    Each service starts from the projects it owns (those whose deepest
    enclosing boundary is the service) and follows their references:
    a library is consumed, and its own references followed in turn; a
    reference to another service, a test or an aggregator stops there.
    """
    services = set(service_roots)
    by_root = {project.root: project for project in projects}
    carved = {
        project.root: project.kind
        for project in projects
        if project.kind in _CARVED_KINDS and project.root not in services
    }
    layout = RepositoryLayout(
        service_roots=list(service_roots),
        carved=carved,
        project_files={project.root: project.file_path for project in projects},
    )
    consumers: dict[str, set[str]] = {}
    for service in service_roots:
        pending = [
            project.root
            for project in projects
            if layout._deepest(project.root) == (service, None)
        ]
        seen: set[str] = set(pending)
        while pending:
            project = by_root.get(pending.pop())
            if project is None:
                continue
            for reference in project.references:
                if reference in seen or reference in services:
                    continue
                seen.add(reference)
                if carved.get(reference) is ProjectKind.LIBRARY:
                    consumers.setdefault(reference, set()).add(service)
                    pending.append(reference)
                elif layout._deepest(reference) == (service, None):
                    pending.append(reference)
    layout.consumers = {root: sorted(users) for root, users in consumers.items()}
    return layout


def unresolved_sites(projects: list[BuildProject]) -> dict[str, list[OpaqueReferenceSite]]:
    """Unresolved project references by build file path."""
    return {project.file_path: project.unresolved for project in projects if project.unresolved}


def dotnet_project_roots(file_paths: list[str]) -> frozenset[str]:
    """Directories holding a ``*.csproj``: their ``bin/`` is build output."""
    return frozenset(
        _directory(path) for path in file_paths if path.lower().endswith(".csproj")
    )

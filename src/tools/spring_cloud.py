"""Spring Cloud wiring between services of one repository.

Pure functions the repository scanner calls once it has read every Spring
configuration file (``application*``/``bootstrap*``) of every service.

- **Config server.** A service whose configuration sets
  ``spring.cloud.config.server.native.search-locations`` to ``classpath:/x``
  serves the files in its ``src/main/resources/x`` directory:
  ``<app>[-<profile>].yml|yaml|properties`` to the service whose
  ``spring.application.name`` is ``<app>``, and ``application[-<profile>]``
  to every client. Those files are the clients' configuration, not the
  server's.
- **Config clients.** A service with ``spring.cloud.config.uri`` or
  ``spring.config.import: configserver:...`` reads its configuration from a
  config server. When that server is a service of the same repository with
  native search locations, its configuration is read here; otherwise it
  stays unknown (``EXTERNAL_CONFIG_SOURCE``).
- **Zuul routes.** ``zuul.routes.<r>.serviceId`` calls a service through
  the registry (as ``lb://<serviceId>``); ``zuul.routes.<r>.url`` is an
  ordinary address key.
"""

import os
import re
from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel, Field

from src.core.env_names import endpoint_in_value
from src.core.service_ids import host_of

_SEARCH_LOCATIONS: Final[str] = "spring.cloud.config.server.native.searchlocations"
_APPLICATION_NAME: Final[str] = "spring.application.name"
_CONFIG_URI: Final[str] = "spring.cloud.config.uri"
_CONFIG_IMPORT: Final[str] = "spring.config.import"
_RESOURCES: Final[str] = "src/main/resources"
_SERVED_FILE: Final[re.Pattern[str]] = re.compile(r"^(?P<stem>.+)\.(?:ya?ml|properties)$", re.IGNORECASE)
_ZUUL_SERVICE_ID: Final[re.Pattern[str]] = re.compile(r"^zuul\.routes\.[^.]+\.service-?id$", re.IGNORECASE)
_SHARED_STEM: Final[str] = "application"


def _relaxed(key: str) -> str:
    """Spring's relaxed binding: case and dashes don't matter."""
    return key.replace("-", "").replace("_", "").lower()


def _get(properties: Mapping[str, str], key: str) -> str | None:
    wanted = _relaxed(key)
    return next((value for name, value in properties.items() if _relaxed(name) == wanted), None)


class SpringSettings(BaseModel):
    """What a service's own Spring configuration says about Spring Cloud.

    ``config_server`` is the host of the config server it imports from
    (``""`` when the address can't be read), None when it is no config
    client. ``served_directories`` are the repository directories it serves
    as a native config server.
    """

    application_name: str | None = None
    config_server: str | None = None
    served_directories: list[str] = Field(default_factory=list)


def _resources_root(file_path: str) -> str:
    """The resources directory a configuration file is in (or its directory)."""
    directory = os.path.dirname(file_path.lstrip("/"))
    position = directory.find(_RESOURCES)
    return directory[: position + len(_RESOURCES)] if position >= 0 else directory


def served_directory(location: str, file_path: str) -> str | None:
    """The repository directory a ``classpath:`` search location names, or None."""
    location = location.strip()
    if not location.lower().startswith("classpath:"):
        return None
    relative = location.split(":", 1)[1].strip("/")
    root = _resources_root(file_path)
    return "/".join(part for part in (root, relative) if part)


def _config_server_host(properties: Mapping[str, str]) -> str | None:
    uri = _get(properties, _CONFIG_URI)
    imported = _get(properties, _CONFIG_IMPORT) or ""
    if "configserver:" in imported.lower():
        uri = uri or imported.split("configserver:", 1)[1]
    if uri is None:
        return None
    endpoint = endpoint_in_value(uri)
    return host_of(endpoint).lower() if endpoint else ""


def spring_settings(files: Mapping[str, Mapping[str, str]]) -> SpringSettings:
    """Settings from a service's base-profile Spring files (path -> properties)."""
    settings = SpringSettings()
    for path, properties in sorted(files.items()):
        name = _get(properties, _APPLICATION_NAME)
        if name and "${" not in name and settings.application_name is None:
            settings.application_name = name.strip()
        host = _config_server_host(properties)
        if host is not None and not settings.config_server:
            settings.config_server = host
        for location in (_get(properties, _SEARCH_LOCATIONS) or "").split(","):
            directory = served_directory(location, path)
            if directory and directory not in settings.served_directories:
                settings.served_directories.append(directory)
    return settings


class ServedFile(BaseModel):
    """A file a config server serves: to whom, under which profile.

    ``clients`` are service roots; ``application`` is None for the file
    every client reads.
    """

    application: str | None = None
    profile: str | None = None
    clients: list[str] = Field(default_factory=list)


def _split_stem(stem: str, applications: list[str]) -> tuple[str, str | None] | None:
    """``(application, profile)`` for a served file's stem, longest name first."""
    for name in sorted(applications, key=len, reverse=True):
        if stem.lower() == name.lower():
            return name, None
        if stem.lower().startswith(name.lower() + "-"):
            return name, stem[len(name) + 1:]
    return None


def served_file(path: str, served: Mapping[str, list[str]], clients: Mapping[str, str | None]) -> ServedFile | None:
    """Who reads ``path`` when a config server serves it, or None.

    ``served`` maps each served directory to the roots of the clients of
    its server; ``clients`` maps a client root to its application name.
    """
    relative = path.lstrip("/")
    directory, filename = os.path.split(relative)
    if directory not in served:
        return None
    match = _SERVED_FILE.match(filename)
    if match is None:
        return None
    stem = match.group("stem")
    roots = served[directory]
    if stem.lower() == _SHARED_STEM or stem.lower().startswith(_SHARED_STEM + "-"):
        profile = stem[len(_SHARED_STEM) + 1:] or None
        return ServedFile(profile=profile, clients=list(roots))
    names = {name for root in roots if (name := clients.get(root))}
    split = _split_stem(stem, sorted(names))
    if split is None:
        return ServedFile(application=stem)
    name, profile = split
    return ServedFile(
        application=name, profile=profile, clients=[root for root in roots if clients.get(root) == name]
    )


def config_servers(settings: Mapping[str, SpringSettings], names: Mapping[str, str]) -> dict[str, list[str]]:
    """Served directory -> roots of the clients its in-repository server serves.

    ``names`` maps each service root to its service name. A client is
    served by an in-repository server when the host of its config server
    address is that server's service name or application name; with one
    native server in the repository and an unreadable address, it is that
    one.
    """
    servers = {root: s for root, s in settings.items() if s.served_directories}
    served: dict[str, list[str]] = {}
    for server_root, server in servers.items():
        aliases = {names.get(server_root, "").lower(), (server.application_name or "").lower()} - {""}
        for client_root, client in settings.items():
            if client.config_server is None or client_root == server_root:
                continue
            if client.config_server in aliases or (client.config_server == "" and len(servers) == 1):
                for directory in server.served_directories:
                    served.setdefault(directory, []).append(client_root)
    return served


def zuul_route_services(properties: Mapping[str, str]) -> list[str]:
    """Services a Zuul gateway routes to through the registry."""
    return [
        value.strip()
        for key, value in properties.items()
        if _ZUUL_SERVICE_ID.match(key) and value.strip() and "${" not in value
    ]

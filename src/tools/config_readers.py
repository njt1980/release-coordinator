"""Read configuration frameworks out of source files.

Pure functions the repository scanner calls per file. Each returns
``ConfigBinding`` records: a configuration key a service reads as a
dependency address, the environment variable that overrides it, and the
defaults source control declares for it. Which value the service actually
runs with is decided later, against the cluster (see
``src.core.config_resolution``).

Understood:

- .NET: ``appsettings.json`` and ``appsettings.<Environment>.json`` (keys
  flattened to ``A:B``, overridden by ``A__B``); in C#,
  ``configuration["A:B"]``, ``GetValue<T>("A:B", default)``,
  ``GetConnectionString("n")`` and .NET Aspire client integrations
  (``AddRedisClient("n")``...), which read ``ConnectionStrings:n``.
- Spring: ``application[-<profile>].yml|yaml|properties`` and
  ``bootstrap*`` (multi-document YAML with ``spring.config.activate.on-profile``),
  ``${NAME:default}`` placeholders, relaxed binding; in Java,
  ``@Value("${a.b:default}")`` and ``getProperty("a.b", default)``.
- Defaults in code: ``os.getenv("X", "d")``, ``process.env.X || "d"``,
  ``System.getenv().getOrDefault("X", "d")``,
  ``Environment.GetEnvironmentVariable("X") ?? "d"``, ``ENV.fetch("X", "d")``.
- Dockerfile ``ENV`` in the final stage and the stages it builds on.

Keys that configure the service's own listener (Kestrel, ``urls``,
Spring ``server.*``) are never dependencies.
"""

import json
import os
import re
import shlex
from enum import Enum
from typing import Any, Final

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from src.core.config_resolution import (
    dotnet_variable,
    placeholders,
    spring_variable,
)
from src.core.env_names import (
    endpoint_in_value,
    has_credential_field,
    is_endpoint_variable,
)
from src.core.models import (
    ConfigBinding,
    ConfigFramework,
    StaticDefault,
    StaticDefaultSource,
    storable_variable_value,
)


class ConfigFileKind(str, Enum):
    """A file a configuration framework reads."""

    DOTNET_SETTINGS = "DOTNET_SETTINGS"
    SPRING_CONFIG = "SPRING_CONFIG"
    DOCKERFILE = "DOCKERFILE"


class ConfigFile(BaseModel):
    """What kind of configuration file a path is, and its profile."""

    kind: ConfigFileKind
    profile: str | None = None


class ConfigParseError(ValueError):
    """A framework configuration file that can't be parsed."""


class ImageEnvironment(BaseModel):
    """The ``ENV`` a Dockerfile gives its final image.

    ``variables`` holds every literal value (callers decide what to keep);
    ``opaque`` names values built from ``ARG`` or other expansions.
    """

    variables: dict[str, str] = Field(default_factory=dict)
    opaque: list[str] = Field(default_factory=list)


_DOTNET_SETTINGS: Final[re.Pattern[str]] = re.compile(
    r"^appsettings(?:\.(?P<profile>[^.]+))?\.json$", re.IGNORECASE
)
_SPRING_CONFIG: Final[re.Pattern[str]] = re.compile(
    r"^(?:application|bootstrap)(?:-(?P<profile>[^.]+))?\.(?:ya?ml|properties)$",
    re.IGNORECASE,
)
# Keys that configure the service's own listener, not a dependency.
_DOTNET_LISTEN_SECTIONS: Final[frozenset[str]] = frozenset(
    {"kestrel", "urls", "http_ports", "https_ports", "allowedhosts"}
)
_SPRING_LISTEN_PREFIXES: Final[tuple[str, ...]] = ("server.", "management.server.")
_CONNECTION_STRINGS: Final[str] = "connectionstrings"

# A string literal: "..." or '...' (no escapes across quotes).
_STRING: Final[str] = r"""(?:"(?P<dq>[^"\\\n]*)"|'(?P<sq>[^'\\\n]*)')"""
_NAME: Final[str] = r"""['"`](?P<name>[A-Za-z_][A-Za-z0-9_]*)['"`]"""
_KEY: Final[str] = r'"(?P<key>[A-Za-z0-9_.:\-\[\]]+)"'

# Environment reads with a literal default, any language the scanner reads.
_ENV_DEFAULT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        # Python: os.getenv("X", "d"), os.environ.get("X", "d")
        rf"(?<![\w$.])(?:os\.)?(?:getenv\(|environ\.get\()\s*{_NAME}\s*,\s*{_STRING}",
        # JavaScript/TypeScript: process.env.X || "d", process.env["X"] ?? "d"
        rf"\b(?:process|import\.meta)\.env\.(?P<name2>[A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\||\?\?)\s*{_STRING}",
        rf"\b(?:process|import\.meta)\.env\[\s*{_NAME}\s*\]\s*(?:\|\||\?\?)\s*{_STRING}",
        # Java: System.getenv().getOrDefault("X", "d")
        rf"\bSystem\.getenv\(\)\.getOrDefault\(\s*{_NAME}\s*,\s*{_STRING}",
        # C#: Environment.GetEnvironmentVariable("X") ?? "d"
        rf"\bEnvironment\.GetEnvironmentVariable\(\s*{_NAME}\s*\)\s*\?\?\s*{_STRING}",
        # Ruby: ENV.fetch("X", "d"), ENV["X"] || "d"
        rf"(?<![\w.])ENV\.fetch\(\s*{_NAME}\s*,\s*{_STRING}",
        rf"(?<![\w.])ENV\[\s*{_NAME}\s*\]\s*\|\|\s*{_STRING}",
    )
)
_OPTIONAL_DEFAULT: Final[str] = rf"(?:\s*\?\?\s*{_STRING})?"
# C# IConfiguration reads by literal key.
_DOTNET_KEY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        # configuration["A:B"], _config["A:B"], builder.Configuration["A:B"]
        rf"\b\w*[Cc]onfig\w*\s*\[\s*{_KEY}\s*\]{_OPTIONAL_DEFAULT}",
        # GetValue<string>("A:B"), GetValue<string?>("A:B", "d")
        rf"\.GetValue<[^>]+>\(\s*{_KEY}\s*(?:,\s*{_STRING})?",
    )
)
_DOTNET_CONNECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        rf"\.GetConnectionString\(\s*{_KEY}\s*\){_OPTIONAL_DEFAULT}",
        # .NET Aspire client integrations read ConnectionStrings:<name>
        # (AddHttpClient's name is a client name, not a connection).
        r"\.Add(?:Keyed)?(?!Http)[A-Z]\w*?(?:Client|DbContext|DataSource|Producer|Consumer|"
        rf"DistributedCache|OutputCache)(?:<[^>()]*>)?\(\s*{_KEY}",
    )
)
# Java (Spring): @Value("${a.b:default}"), environment.getProperty("a.b", "d").
_SPRING_VALUE: Final[re.Pattern[str]] = re.compile(r'@Value\(\s*"(?P<expr>\$\{[^"]*\})"\s*\)')
_SPRING_GET_PROPERTY: Final[re.Pattern[str]] = re.compile(
    rf"\.getProperty\(\s*{_KEY}\s*(?:,\s*{_STRING})?"
)
# Spring Cloud DiscoveryClient: discoveryClient.getInstances("vets-service").
_DISCOVERY_LOOKUP: Final[re.Pattern[str]] = re.compile(
    r'\.getInstances\(\s*"(?P<name>[A-Za-z0-9-]+)"\s*\)'
)
# Spring Cloud OpenFeign: @FeignClient(name = "customers-service").
_FEIGN_CLIENT: Final[re.Pattern[str]] = re.compile(
    r'@FeignClient\(\s*(?:(?:name|value)\s*=\s*)?"(?P<name>[A-Za-z0-9-]+)"'
)


def config_file(path: str) -> ConfigFile | None:
    """Classify ``path`` as a framework configuration file, or None."""
    filename = os.path.basename(path)
    if filename.lower() == "dockerfile":
        return ConfigFile(kind=ConfigFileKind.DOCKERFILE)
    match = _DOTNET_SETTINGS.match(filename)
    if match:
        return ConfigFile(kind=ConfigFileKind.DOTNET_SETTINGS, profile=match.group("profile"))
    match = _SPRING_CONFIG.match(filename)
    if match:
        return ConfigFile(kind=ConfigFileKind.SPRING_CONFIG, profile=match.group("profile"))
    return None


def _string(match: re.Match[str]) -> str | None:
    groups = match.groupdict()
    value = groups.get("dq")
    return value if value is not None else groups.get("sq")


def storable_template(value: str, connection_string: bool) -> str | None:
    """Return the credential-free form of a default a snapshot may keep.

    Without placeholders, the endpoint the value names (None if none);
    with ``${...}`` placeholders, the value with user info and query
    removed (None if it still carries a credential).
    """
    if "${" not in value:
        return endpoint_in_value(value, connection_string=connection_string)
    cleaned = re.sub(r"//[^/@\s]*@", "//", value)
    cleaned = re.sub(r"\?[^}]*$", "", cleaned)
    return None if has_credential_field(cleaned) else cleaned


# --- .NET ------------------------------------------------------------------


def _strip_json_comments(content: str) -> str:
    """Remove // and /* */ comments and trailing commas outside strings."""
    out: list[str] = []
    position, in_string = 0, False
    while position < len(content):
        char = content[position]
        if in_string:
            out.append(char)
            if char == "\\" and position + 1 < len(content):
                out.append(content[position + 1])
                position += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif content.startswith("//", position):
            end = content.find("\n", position)
            position = len(content) if end < 0 else end
            continue
        elif content.startswith("/*", position):
            end = content.find("*/", position + 2)
            position = len(content) if end < 0 else end + 2
            continue
        else:
            out.append(char)
        position += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _flatten(node: Any, prefix: str, separator: str, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten(value, f"{prefix}{separator}{key}" if prefix else str(key), separator, out)
    elif isinstance(node, list):
        for position, value in enumerate(node):
            if separator == ":":
                _flatten(value, f"{prefix}:{position}", separator, out)
            else:
                _flatten(value, f"{prefix}[{position}]", separator, out)
    elif node is not None and not isinstance(node, bool):
        out[prefix] = str(node)


def flatten_dotnet_settings(content: str) -> dict[str, str]:
    """Flatten an appsettings file to ``Section:Key`` -> value.

    Comments and trailing commas (which .NET accepts) are allowed.

    Raises:
        ConfigParseError: if the file isn't a JSON object.
    """
    try:
        document = json.loads(_strip_json_comments(content.lstrip("﻿")))
    except json.JSONDecodeError as error:
        raise ConfigParseError(str(error)) from error
    if not isinstance(document, dict):
        raise ConfigParseError("appsettings must be a JSON object")
    flat: dict[str, str] = {}
    _flatten(document, "", ":", flat)
    return flat


def _is_dotnet_listen_key(key: str) -> bool:
    return key.split(":", 1)[0].lower() in _DOTNET_LISTEN_SECTIONS


def _dotnet_binding(
    key: str, default: str | None, source: StaticDefaultSource, file_path: str, profile: str | None
) -> ConfigBinding | None:
    """A binding for a .NET key when it is (or holds) a dependency address."""
    if _is_dotnet_listen_key(key):
        return None
    connection_string = key.lower().startswith(_CONNECTION_STRINGS + ":")
    template = storable_template(default, connection_string) if default else None
    if not (connection_string or is_endpoint_variable(key.rsplit(":", 1)[-1]) or template):
        return None
    defaults = (
        [StaticDefault(template=template, source=source, file_path=file_path, profile=profile)]
        if template
        else []
    )
    return ConfigBinding(
        variable=dotnet_variable(key), key=key, framework=ConfigFramework.DOTNET, defaults=defaults
    )


def dotnet_settings_bindings(content: str, file_path: str, profile: str | None) -> list[ConfigBinding]:
    """Bindings an appsettings file declares (see ``_dotnet_binding``).

    Raises:
        ConfigParseError: if the file can't be parsed.
    """
    bindings = [
        _dotnet_binding(key, value, StaticDefaultSource.CONFIG_FILE, file_path, profile)
        for key, value in flatten_dotnet_settings(content).items()
    ]
    return [binding for binding in bindings if binding is not None]


def dotnet_code_bindings(content: str, file_path: str) -> list[ConfigBinding]:
    """Bindings C# code reads through IConfiguration with literal keys."""
    found: list[ConfigBinding | None] = []
    for pattern in _DOTNET_KEY_PATTERNS:
        for match in pattern.finditer(content):
            found.append(
                _dotnet_binding(match.group("key"), _string(match), StaticDefaultSource.CODE, file_path, None)
            )
    for pattern in _DOTNET_CONNECTION_PATTERNS:
        for match in pattern.finditer(content):
            key = f"ConnectionStrings:{match.group('key')}"
            found.append(_dotnet_binding(key, _string(match), StaticDefaultSource.CODE, file_path, None))
    return [binding for binding in found if binding is not None]


# --- Spring ----------------------------------------------------------------


def _spring_profile(document: dict[str, Any]) -> str | None:
    """The profile a YAML document activates on, if any."""
    flat: dict[str, str] = {}
    _flatten(document, "", ".", flat)
    return flat.get("spring.config.activate.on-profile") or flat.get("spring.profiles")


def _parse_properties(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        match = re.match(r"^([^=:\s]+)\s*[=:]\s*(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def spring_documents(content: str, file_path: str, profile: str | None) -> list[tuple[str | None, dict[str, str]]]:
    """Flatten a Spring config file to ``(profile, {a.b: value})`` documents.

    Raises:
        ConfigParseError: if YAML can't be parsed.
    """
    if file_path.lower().endswith(".properties"):
        return [(profile, _parse_properties(content))]
    try:
        documents = [d for d in yaml.safe_load_all(content) if d is not None]
    except yaml.YAMLError as error:
        raise ConfigParseError(str(error)) from error
    result: list[tuple[str | None, dict[str, str]]] = []
    for document in documents:
        if not isinstance(document, dict):
            raise ConfigParseError("a Spring YAML document must be a mapping")
        flat: dict[str, str] = {}
        _flatten(document, "", ".", flat)
        result.append((_spring_profile(document) or profile, flat))
    return result


def _inline_self_references(value: str, properties: dict[str, str]) -> str:
    """Give ``${other.key}`` placeholders the file's value of that key as default."""
    parts: list[str] = []
    position = 0
    for name, default, start, end in placeholders(value):
        parts.append(value[position:start])
        position = end
        if default is None and name in properties and "${" not in properties[name]:
            parts.append(f"${{{name}:{properties[name]}}}")
        else:
            parts.append(value[start:end])
    parts.append(value[position:])
    return "".join(parts)


def _spring_default_endpoint(value: str) -> str | None:
    """The address a Spring value names once its placeholder defaults are filled."""
    return endpoint_in_value(fill_placeholder_defaults(value))


def fill_placeholder_defaults(value: str) -> str:
    """Replace each placeholder by its default (empty without one)."""
    out: list[str] = []
    position = 0
    for _, default, start, end in placeholders(value):
        out.append(value[position:start])
        out.append(fill_placeholder_defaults(default) if default is not None else "")
        position = end
    out.append(value[position:])
    return "".join(out)


def _spring_binding(
    key: str, value: str | None, source: StaticDefaultSource, file_path: str, profile: str | None
) -> ConfigBinding | None:
    """A binding for a Spring property when it is (or holds) a dependency address."""
    if key.lower().startswith(_SPRING_LISTEN_PREFIXES):
        return None
    template = storable_template(value, connection_string=False) if value else None
    names_address = is_endpoint_variable(key.rsplit(".", 1)[-1])
    holds_address = bool(value) and (
        _spring_default_endpoint(value or "") is not None
        or any(is_endpoint_variable(name.rsplit(".", 1)[-1]) for name, *_ in placeholders(value or ""))
    )
    if not (names_address or holds_address):
        return None
    defaults = (
        [StaticDefault(template=template, source=source, file_path=file_path, profile=profile)]
        if template
        else []
    )
    return ConfigBinding(
        variable=spring_variable(key), key=key, framework=ConfigFramework.SPRING, defaults=defaults
    )


def spring_config_bindings(content: str, file_path: str, profile: str | None) -> list[ConfigBinding]:
    """Bindings a Spring configuration file declares.

    Raises:
        ConfigParseError: if the file can't be parsed.
    """
    bindings: list[ConfigBinding] = []
    for document_profile, properties in spring_documents(content, file_path, profile):
        for key, value in properties.items():
            binding = _spring_binding(
                key,
                _inline_self_references(value, properties),
                StaticDefaultSource.CONFIG_FILE,
                file_path,
                document_profile,
            )
            if binding is not None:
                bindings.append(binding)
    return bindings


def spring_code_bindings(content: str, file_path: str) -> list[ConfigBinding]:
    """Bindings Java code reads through ``@Value`` and ``getProperty``."""
    found: list[ConfigBinding | None] = []
    for match in _SPRING_VALUE.finditer(content):
        for name, default, _, _ in placeholders(match.group("expr")):
            found.append(_spring_binding(name, default, StaticDefaultSource.CODE, file_path, None))
    for match in _SPRING_GET_PROPERTY.finditer(content):
        found.append(
            _spring_binding(match.group("key"), _string(match), StaticDefaultSource.CODE, file_path, None)
        )
    return [binding for binding in found if binding is not None]


def feign_client_services(content: str) -> list[str]:
    """Service names Spring code calls through the service registry:
    OpenFeign clients (``@FeignClient``) and ``DiscoveryClient``
    lookups (``getInstances("name")``)."""
    matches = [*_FEIGN_CLIENT.finditer(content), *_DISCOVERY_LOOKUP.finditer(content)]
    matches.sort(key=lambda match: match.start())
    return list(dict.fromkeys(match.group("name") for match in matches))


# --- Environment defaults in code ------------------------------------------


def env_default_bindings(content: str, file_path: str) -> list[ConfigBinding]:
    """Bindings for environment variables code reads with a literal default.

    A variable counts when its name is endpoint-like or its default names
    an address.
    """
    bindings: list[ConfigBinding] = []
    for pattern in _ENV_DEFAULT_PATTERNS:
        for match in pattern.finditer(content):
            groups = match.groupdict()
            name = groups.get("name") or groups.get("name2") or ""
            default = _string(match)
            template = storable_template(default, connection_string=False) if default else None
            if not name or not (template or is_endpoint_variable(name)):
                continue
            bindings.append(
                ConfigBinding(
                    variable=name,
                    key=name,
                    framework=ConfigFramework.ENV,
                    defaults=[
                        StaticDefault(template=template, source=StaticDefaultSource.CODE, file_path=file_path)
                    ]
                    if template
                    else [],
                )
            )
    return bindings


def merge_bindings(bindings: list[ConfigBinding]) -> list[ConfigBinding]:
    """Merge bindings of the same variable, keeping every distinct default.

    .NET variables merge case-insensitively. The first binding's spelling
    wins. Order is first appearance.
    """
    merged: dict[tuple[ConfigFramework, str], ConfigBinding] = {}
    for binding in bindings:
        name = binding.variable.lower() if binding.framework is ConfigFramework.DOTNET else binding.variable
        key = (binding.framework, name)
        existing = merged.get(key)
        if existing is None:
            merged[key] = binding.model_copy(deep=True)
            continue
        for default in binding.defaults:
            if default not in existing.defaults:
                existing.defaults.append(default)
    return list(merged.values())


# --- Dockerfile --------------------------------------------------------------


def _dockerfile_instructions(content: str) -> list[tuple[str, str]]:
    """``(INSTRUCTION, arguments)`` pairs with continuation lines joined."""
    instructions: list[tuple[str, str]] = []
    pending = ""
    for raw in content.splitlines():
        line = raw.rstrip()
        if not pending and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = pending + line
        pending = ""
        parts = line.strip().split(None, 1)
        if parts:
            instructions.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    return instructions


def _env_pairs(arguments: str) -> list[tuple[str, str]]:
    """Parse ``ENV`` arguments: ``K=V K2="v 2"`` or the legacy ``K V``."""
    try:
        tokens = shlex.split(arguments, posix=True)
    except ValueError:
        return []
    if tokens and "=" not in tokens[0]:
        return [(tokens[0], " ".join(tokens[1:]))]
    pairs = []
    for token in tokens:
        name, separator, value = token.partition("=")
        if separator:
            pairs.append((name, value))
    return pairs


def dockerfile_environment(content: str) -> ImageEnvironment:
    """The ``ENV`` of a Dockerfile's final image.

    The final stage's ``ENV`` and that of the earlier stages it is built
    ``FROM`` (by alias), in order. A value that expands ``$VAR`` is opaque.
    """
    stages: list[tuple[str | None, str, list[tuple[str, str]]]] = []
    for instruction, arguments in _dockerfile_instructions(content):
        if instruction == "FROM":
            words = arguments.split()
            words = [w for w in words if not w.startswith("--")]
            base = words[0].lower() if words else ""
            alias = words[2].lower() if len(words) >= 3 and words[1].lower() == "as" else None
            stages.append((alias, base, []))
        elif instruction == "ENV" and stages:
            stages[-1][2].extend(_env_pairs(arguments))
    if not stages:
        return ImageEnvironment()
    chain: list[list[tuple[str, str]]] = []
    by_alias = {alias: index for index, (alias, _, _) in enumerate(stages) if alias}
    index: int | None = len(stages) - 1
    seen: set[int] = set()
    while index is not None and index not in seen:
        seen.add(index)
        chain.append(stages[index][2])
        index = by_alias.get(stages[index][1])
    image = ImageEnvironment()
    opaque: set[str] = set()
    for pairs in reversed(chain):
        for name, value in pairs:
            if re.search(r"(?<!\\)\$", value):
                opaque.add(name)
                image.variables.pop(name, None)
            else:
                opaque.discard(name)
                image.variables[name] = value
    image.opaque = sorted(opaque)
    return image


def storable_image_environment(image: ImageEnvironment) -> tuple[dict[str, str], list[str]]:
    """Split an image's ``ENV`` into values a snapshot may keep and withheld names."""
    kept: dict[str, str] = {}
    withheld: list[str] = []
    for name, value in image.variables.items():
        cleaned = storable_variable_value(name, value)
        if cleaned is None:
            withheld.append(name)
        else:
            kept[name] = cleaned
    return kept, sorted(withheld)

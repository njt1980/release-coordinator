"""Read reverse-proxy configurations (nginx, Envoy) for upstream addresses.

Pure functions the repository scanner calls per file. A gateway or web
front end often has no application code of its own: its dependencies are
the upstreams its proxy configuration forwards to.

Understood:

- nginx (``nginx.conf``, ``*.conf`` and envsubst templates such as
  ``default.conf.template``): ``proxy_pass``/``grpc_pass`` URLs and the
  ``server`` entries of ``upstream`` blocks. ``proxy_pass`` to an upstream
  block's name is that block's servers. A host written as an envsubst
  placeholder (``http://${CATALOGUE_HOST}:8080``) is that environment
  variable, resolved later like any other. A host held in an nginx
  variable (``proxy_pass http://$backend``) is not understood.
- Envoy (``envoy.yaml``): every ``socket_address`` (cluster endpoints;
  listener addresses such as ``0.0.0.0`` are not dependencies and are
  dropped by the scanner's host filter).
"""

import os
import re
from typing import Any, Final

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from src.core.models import OpaqueReferenceKind, OpaqueReferenceSite

_NGINX_FILENAMES: Final[frozenset[str]] = frozenset({"nginx.conf", "default.conf"})
_ENVOY_FILENAMES: Final[frozenset[str]] = frozenset({"envoy.yaml", "envoy.yml"})
# envsubst templates: the suffix before ``.template`` says what the file is.
_TEMPLATE_SUFFIX: Final[str] = ".template"

_DIRECTIVE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?P<name>proxy_pass|grpc_pass|server)[ \t]+(?P<value>[^;#\n]+);", re.MULTILINE
)
_UPSTREAM_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*upstream[ \t]+(?P<name>[\w.-]+)[ \t]*\{(?P<body>[^}]*)\}", re.MULTILINE
)
_NGINX_HINT: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(?:proxy_pass|grpc_pass|upstream)\b", re.MULTILINE)
# scheme://host[:port]... where host may be an envsubst placeholder.
_PASS_URL: Final[re.Pattern[str]] = re.compile(
    r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<host>\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_]\w*|[^/:$\s]+)"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}$")
_SCHEMES: Final[dict[str, str]] = {"grpc_pass": "grpc"}


class ProxyRead(BaseModel):
    """What one proxy configuration forwards to.

    ``urls`` are literal upstream addresses (``scheme://host[:port]``);
    ``variables`` environment variables an envsubst template puts in an
    upstream host; ``sites`` upstreams held in a proxy variable, which the
    scanner can't follow.
    """

    urls: list[str] = Field(default_factory=list)
    variables: list[str] = Field(default_factory=list)
    sites: list[OpaqueReferenceSite] = Field(default_factory=list)


def _base_filename(path: str) -> str:
    filename = os.path.basename(path).lower()
    if filename.endswith(_TEMPLATE_SUFFIX):
        filename = filename[: -len(_TEMPLATE_SUFFIX)]
    return filename


def is_envoy_config(path: str) -> bool:
    """Whether ``path`` is named like an Envoy bootstrap configuration."""
    return _base_filename(path) in _ENVOY_FILENAMES


def is_nginx_candidate(path: str) -> bool:
    """Whether ``path`` may hold nginx configuration (content decides)."""
    filename = _base_filename(path)
    return filename in _NGINX_FILENAMES or filename.endswith(".conf")


def template_suffix(path: str) -> str | None:
    """The suffix an envsubst template (``x.conf.template``) renders to, or None."""
    filename = os.path.basename(path).lower()
    if not filename.endswith(_TEMPLATE_SUFFIX):
        return None
    return os.path.splitext(filename[: -len(_TEMPLATE_SUFFIX)])[1] or None


def is_nginx_config(content: str) -> bool:
    """Whether ``content`` uses nginx proxy directives."""
    return bool(_NGINX_HINT.search(content))


def _line(content: str, position: int) -> int:
    return content.count("\n", 0, position) + 1


def _add_target(read: ProxyRead, scheme: str, value: str, line: int) -> None:
    match = _PASS_URL.match(value.strip())
    if match is None:
        return
    host = match.group("host")
    placeholder = _PLACEHOLDER.match(host)
    if placeholder:
        read.variables.append(placeholder.group("name"))
    elif host.startswith("$"):
        read.sites.append(OpaqueReferenceSite(line=line, kind=OpaqueReferenceKind.CLIENT_ADDRESS_EXPRESSION))
    else:
        port = re.match(r"^:(\d+)", match.group("rest"))
        read.urls.append(f"{scheme or match.group('scheme')}://{host}" + (f":{port.group(1)}" if port else ""))


def nginx_upstreams(content: str) -> ProxyRead:
    """Upstream addresses an nginx configuration forwards to."""
    read = ProxyRead()
    upstreams: set[str] = set()
    in_upstream: list[tuple[int, int]] = []
    for block in _UPSTREAM_BLOCK.finditer(content):
        upstreams.add(block.group("name").lower())
        in_upstream.append(block.span())
        for server in re.finditer(r"^[ \t]*server[ \t]+(?P<addr>[^\s;]+)", block.group("body"), re.MULTILINE):
            address = server.group("addr")
            if not address.startswith(("unix:", "$")):
                _add_target(read, "http", f"http://{address}", _line(content, block.start("body") + server.start()))
    for directive in _DIRECTIVE.finditer(content):
        name = directive.group("name")
        if name == "server" or any(start <= directive.start() < end for start, end in in_upstream):
            continue
        value = directive.group("value").strip()
        match = _PASS_URL.match(value)
        if match and match.group("host").lower() in upstreams:
            continue
        _add_target(read, _SCHEMES.get(name, ""), value, _line(content, directive.start()))
    read.urls = list(dict.fromkeys(read.urls))
    read.variables = list(dict.fromkeys(read.variables))
    return read


def _socket_addresses(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        socket = node.get("socket_address")
        if isinstance(socket, dict) and isinstance(socket.get("address"), str):
            port = socket.get("port_value")
            found.append(f"http://{socket['address']}" + (f":{port}" if isinstance(port, int) else ""))
        for value in node.values():
            _socket_addresses(value, found)
    elif isinstance(node, list):
        for value in node:
            _socket_addresses(value, found)


def envoy_upstreams(content: str) -> ProxyRead:
    """Upstream addresses an Envoy configuration forwards to.

    A file that isn't valid YAML yields nothing (its URLs are still read as
    any other file's).
    """
    try:
        documents = list(yaml.safe_load_all(content))
    except yaml.YAMLError:
        return ProxyRead()
    found: list[str] = []
    for document in documents:
        _socket_addresses(document, found)
    return ProxyRead(urls=list(dict.fromkeys(found)))


def proxy_upstreams(content: str, path: str) -> ProxyRead | None:
    """The upstreams ``path`` forwards to, or None if it is no proxy config."""
    if is_envoy_config(path):
        return envoy_upstreams(content)
    if is_nginx_candidate(path) and is_nginx_config(content):
        return nginx_upstreams(content)
    return None

"""What an environment variable's name and value say about a dependency.

Pure helpers shared by the code scanner (which names count as endpoints,
what address a configured value names) and the snapshot boundary (which
values may be stored).
"""

import re
from typing import Final
from urllib.parse import urlsplit, urlunsplit

# Final name tokens of variables that hold the address of another service.
_ENDPOINT_ENV_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"ADDR", "ADDRESS", "API", "BROKER", "BROKERS", "CONNECTION", "DSN", "ENDPOINT",
     "ENDPOINTS", "HOST", "HOSTNAME", "SERVER", "SERVERS", "URI", "URL", "URLS"}
)
# Final name tokens whose value may be a bare host[:port] (see names_host).
_HOST_ENV_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"ADDR", "ADDRESS", "BROKER", "BROKERS", "ENDPOINT", "ENDPOINTS", "HOST", "HOSTNAME",
     "SERVER", "SERVERS"}
)
_ENV_NAME_TOKEN_SPLIT:Final[re.Pattern[str]] = re.compile(
    r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])"
)
# .NET configuration sections whose every key is an address:
# ConnectionStrings__<name>, and Aspire service discovery's
# services__<service>__<endpoint>__<n>.
_ADDRESS_SECTIONS: Final[frozenset[str]] = frozenset({"connectionstrings", "services"})
# Key/value connection-string fields that carry a credential
# (Server=x;Password=y, AccountKey=..., SharedAccessKey=...).
_SECRET_FIELD: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[;,&?\s])(?:password|pwd|passwd|secret|token|accountkey|sharedaccesskey|"
    r"sharedaccesssignature|sig|apikey|api[_-]?key|access[_-]?key)\s*=",
    re.IGNORECASE,
)
# Variables that select a configuration profile, so a stored snapshot keeps
# their values: which appsettings.<Environment>.json or application-<profile>
# file applies depends on them. They never hold credentials.
PROFILE_VARIABLES: Final[tuple[str, ...]] = (
    "ASPNETCORE_ENVIRONMENT",
    "DOTNET_ENVIRONMENT",
    "SPRING_PROFILES_ACTIVE",
)
# URL schemes that address another service. ``https+http`` and
# ``http+https`` are .NET Aspire service-discovery schemes, ``lb`` a Spring
# Cloud load-balanced service name, ``dns`` a gRPC target (dns:///host:port).
ENDPOINT_SCHEMES: Final[frozenset[str]] = frozenset(
    {"amqp", "amqps", "dns", "grpc", "grpcs", "http", "http+https", "https", "https+http",
     "kafka", "lb", "mariadb", "mongodb", "mongodb+srv", "mysql", "nats", "postgres",
     "postgresql", "redis", "rediss", "sb", "sqlserver", "tcp"}
)
_SCHEME_URL: Final[re.Pattern[str]] = re.compile(
    r"^(?:jdbc:)?(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<rest>.*)$", re.IGNORECASE | re.DOTALL
)
# Connection-string fields that name the server.
_HOST_FIELDS: Final[frozenset[str]] = frozenset(
    {"addr", "address", "bootstrapservers", "contactpoints", "datasource", "endpoint",
     "host", "hostname", "networkaddress", "server"}
)
_HOST_PORT: Final[re.Pattern[str]] = re.compile(
    r"^(?P<host>\[[0-9a-f:]+\]|[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?)(?::(?P<port>\d{1,5}))?$",
    re.IGNORECASE,
)


def _name_tokens(name: str) -> list[str]:
    return [token for token in _ENV_NAME_TOKEN_SPLIT.split(name) if token]


def is_connection_string_variable(name: str) -> bool:
    """Return True for names whose values are connection strings or addresses.

    ``ConnectionStrings__<name>`` (.NET's environment form of the
    ``ConnectionStrings`` section) and names ending in ``CONNECTION_STRING``.
    """
    first, separator, _ = name.partition("__")
    if separator and first.lower() in _ADDRESS_SECTIONS:
        return True
    tokens = [token.upper() for token in _name_tokens(name)]
    return tokens[-2:] == ["CONNECTION", "STRING"]


def is_endpoint_variable(name: str) -> bool:
    """Return True for variable names that hold another service's address.

    The final name token decides: ``SERVICE_URL``, ``PAYMENTS_API``,
    ``orderServiceHost``, ``KAFKA_BOOTSTRAP_SERVERS`` qualify; ``API_KEY``,
    ``NODE_ENV``, ``PORT`` do not. Connection strings (see
    ``is_connection_string_variable``) always qualify.
    """
    if is_connection_string_variable(name):
        return True
    tokens = _name_tokens(name)
    return bool(tokens) and tokens[-1].upper() in _ENDPOINT_ENV_SUFFIXES


def names_host(name: str) -> bool:
    """Return True for names whose value may be a bare ``host[:port]``.

    Connection strings, and endpoint names ending in a host-like token
    (``CART_HOST=cart``, ``DB_ADDR=mysql:3306``, ``CART_ENDPOINT=cart:8080``,
    ``spring.data.mongodb.host``). ``*_URL``/``*_URI``/``*_API`` values must
    carry a scheme: a bare word there is a path or version, not a host.
    """
    if is_connection_string_variable(name):
        return True
    tokens = _name_tokens(name)
    return bool(tokens) and tokens[-1].upper() in _HOST_ENV_SUFFIXES


def _host_port(raw: str) -> str | None:
    """Return ``host[:port]`` when ``raw`` is exactly that, lower-cased."""
    match = _HOST_PORT.match(raw.strip())
    if match is None:
        return None
    port = match.group("port")
    return match.group("host").lower() + (f":{port}" if port else "")


def _url_endpoint(raw: str) -> str | None:
    """Return ``scheme://host[:port]`` for a URL with an endpoint scheme.

    A gRPC ``dns:`` target becomes its bare ``host[:port]``.
    """
    match = _SCHEME_URL.match(raw)
    if match is None:
        return None
    scheme = match.group("scheme").lower()
    if scheme not in ENDPOINT_SCHEMES:
        return None
    rest = match.group("rest")
    if scheme == "dns":  # dns:///host:port or dns://resolver/host:port
        rest = rest.split("/", 1)[1] if "/" in rest else rest
        return _host_port(rest.split("/", 1)[0])
    # Drop user info, path, query and SQL Server ;properties.
    authority = re.split(r"[/?#;]", rest, maxsplit=1)[0].rsplit("@", 1)[-1]
    # Several hosts (mongodb://a,b): the first one names the dependency.
    endpoint = _host_port(authority.split(",", 1)[0])
    return f"{scheme}://{endpoint}" if endpoint else None


def _connection_string_fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in raw.split(";"):
        key, separator, value = part.partition("=")
        if separator:
            # A PDO DSN prefixes its first field with the driver (mysql:host=db).
            key = re.sub(r"^\s*[a-z]+:", "", key, flags=re.IGNORECASE)
            fields[re.sub(r"\s+", "", key).lower()] = value.strip()
    return fields


def _connection_string_endpoint(raw: str) -> str | None:
    """Return the server a ``Key=Value;...`` connection string names."""
    if "=" not in raw:
        return None
    fields = _connection_string_fields(raw)
    host = next((fields[key] for key in fields if key in _HOST_FIELDS and fields[key]), "")
    if not host:
        return None
    nested = _url_endpoint(host)  # Endpoint=sb://ns.servicebus.windows.net/
    if nested:
        return nested
    # SQL Server: tcp:server,1433 or server\instance; Npgsql: host1,host2.
    host = re.sub(r"^(?:tcp|np|lpc):", "", host, flags=re.IGNORECASE)
    host = host.split("\\", 1)[0]
    name, _, port = host.partition(",")
    if port.strip().isdigit():
        host = f"{name}:{port.strip()}"
    else:
        host = name
    if ":" not in host and fields.get("port", "").isdigit():
        host = f"{host}:{fields['port']}"
    return _host_port(host)


def endpoint_of_value(value: str, connection_string: bool = False) -> str | None:
    """Return the address a configured value names, without credentials.

    A URL with an endpoint scheme (``ENDPOINT_SCHEMES``, also behind
    ``jdbc:``) becomes ``scheme://host[:port]``; a ``Key=Value;`` connection
    string becomes the ``host[:port]`` its server field names. With
    ``connection_string`` (the value is known to be one, such as a
    ``ConnectionStrings`` entry), a bare ``host[:port]`` or StackExchange
    Redis ``host:port,options`` value is accepted too. Anything else is None.
    """
    raw = value.strip()
    if not raw:
        return None
    if _SCHEME_URL.match(raw):
        return _url_endpoint(raw)
    endpoint = _connection_string_endpoint(raw) if ";" in raw or "=" in raw.split(",", 1)[0] else None
    if endpoint is not None or not connection_string:
        return endpoint
    return _host_port(raw.split(",", 1)[0])


def storable_endpoint_value(value: str, connection_string: bool = False) -> str | None:
    """Return an endpoint value with any credential removed, or None.

    URLs keep their scheme, host, port and path; user info, query and
    fragment are dropped. A key/value connection string is reduced to the
    server it names (see ``endpoint_of_value``). A value that still carries
    a credential field can't be cleaned and returns None.
    """
    raw = value.strip()
    if raw.lower().startswith(("jdbc:", "dns:")):
        return _url_endpoint(raw)
    if _SCHEME_URL.match(raw):
        try:
            parts = urlsplit(raw)
            host = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            return None
        netloc = (f"[{host}]" if ":" in host else host) + port
        raw = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    elif connection_string or _SECRET_FIELD.search(raw):
        endpoint = endpoint_of_value(raw, connection_string=connection_string)
        if endpoint is not None:
            return endpoint
    return None if _SECRET_FIELD.search(raw) else raw


_EMBEDDED_URL: Final[re.Pattern[str]] = re.compile(
    r"(?:jdbc:)?[a-z][a-z0-9+.-]*://[^\s\"'<>`,;]+", re.IGNORECASE
)


def endpoint_in_value(value: str, connection_string: bool = False) -> str | None:
    """Return the address a configured value names, or one URL inside it.

    As ``endpoint_of_value``, falling back to the first endpoint URL within
    a longer value (Spring's ``optional:configserver:http://config:8888``).
    """
    endpoint = endpoint_of_value(value, connection_string=connection_string)
    if endpoint is not None:
        return endpoint
    for match in _EMBEDDED_URL.finditer(value):
        endpoint = _url_endpoint(match.group(0))
        if endpoint is not None:
            return endpoint
    return None


_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "::", "host.docker.internal"}
)


def is_loopback_endpoint(endpoint: str) -> bool:
    """Whether an endpoint (as ``endpoint_of_value`` returns) is this host."""
    raw = endpoint.split("://", 1)[-1]
    match = _HOST_PORT.match(raw)
    host = (match.group("host") if match else raw).strip("[]").lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.") or host.endswith(".localhost")


def has_credential_field(value: str) -> bool:
    """Whether a value carries a key/value credential field (``Password=...``)."""
    return bool(_SECRET_FIELD.search(value))

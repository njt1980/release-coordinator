"""What an environment variable's name and value say about a dependency.

Pure helpers shared by the code scanner (which names count as endpoints)
and the snapshot boundary (which values may be stored).
"""

import re
from typing import Final
from urllib.parse import urlsplit, urlunsplit

# Final name tokens of variables that hold the address of another service.
_ENDPOINT_ENV_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"ADDR", "ADDRESS", "API", "DSN", "ENDPOINT", "ENDPOINTS", "HOST", "HOSTNAME",
     "URI", "URL", "URLS"}
)
_ENV_NAME_TOKEN_SPLIT: Final[re.Pattern[str]] = re.compile(
    r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])"
)
# Key/value connection-string fields that carry a credential
# (Server=x;Password=y, AccountKey=..., SharedAccessKey=...).
_SECRET_FIELD: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[;,&?\s])(?:password|pwd|passwd|secret|token|accountkey|sharedaccesskey|"
    r"sharedaccesssignature|sig|apikey|api[_-]?key|access[_-]?key)\s*=",
    re.IGNORECASE,
)


def is_endpoint_variable(name: str) -> bool:
    """Return True for variable names that hold another service's address.

    The final name token decides: ``SERVICE_URL``, ``PAYMENTS_API``,
    ``orderServiceHost`` qualify; ``API_KEY``, ``NODE_ENV``, ``PORT`` do not.
    """
    tokens = [token for token in _ENV_NAME_TOKEN_SPLIT.split(name) if token]
    return bool(tokens) and tokens[-1].upper() in _ENDPOINT_ENV_SUFFIXES


def storable_endpoint_value(value: str) -> str | None:
    """Return an endpoint value with any credential removed, or None.

    URLs keep their scheme, host, port and path; user info, query and
    fragment are dropped. A value that still carries a credential field
    (a key/value connection string) can't be cleaned and returns None.
    """
    raw = value.strip()
    if "://" in raw:
        try:
            parts = urlsplit(raw)
            host = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            return None
        netloc = (f"[{host}]" if ":" in host else host) + port
        raw = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return None if _SECRET_FIELD.search(raw) else raw

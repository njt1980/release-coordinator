"""Find references the scanner reads but can't follow.

Pure functions the repository scanner calls per file. A reference is not
understood when the scanner sees that the code reads configuration, the
environment or a client address, but can't tell which key, variable or
address: a dependency declared there is invisible to the static scan, so
the file's service has incomplete coverage (``REFERENCES_NOT_UNDERSTOOD``).

Recognized, per language the scanner reads:

- Dynamic configuration keys (C#: ``configuration[name]``,
  ``GetValue<T>(name)``, ``GetConnectionString(name)``, ``GetSection(name)``,
  ``BindConfiguration(name)``, an Aspire client's ``AddRedisClient(name)``;
  Java: ``getProperty(name)``) and dynamic environment names
  (``os.getenv(name)``, ``process.env[name]``, ``os.Getenv(name)``,
  ``Environment.GetEnvironmentVariable(name)``, ``System.getenv(name)``,
  ``ENV[name]``). Interpolated strings are dynamic. An environment name
  the same file fixes to literals is followed instead (a loop over a
  literal list, a file-local wrapper called only with literals; see
  ``followed_env_reads``).
- Client addresses (C#: ``BaseAddress = ...``, ``Address = new Uri(...)``,
  ``GrpcChannel.ForAddress(...)``; Java: ``baseUrl(...)``,
  ``WebClient.create(...)``, ``RestClient.create(...)``, ``rootUri(...)``,
  ``ManagedChannelBuilder.forTarget/forAddress(...)``) whose argument is not
  a literal, a configuration or environment read with a literal key, or a
  variable the same file assigns from one (or a Spring ``@Value`` field).
- Externalized configuration (Spring: ``spring.config.import`` from a
  ``configserver:``, ``spring.cloud.config.uri``) in a framework
  configuration file: keys served from there are unknown (see
  ``external_config_sites``).
- Bound sections (C#: ``GetSection("X")``, ``BindConfiguration("X")``,
  ``BindConfiguration(nameof(X))``;
  Java: ``@ConfigurationProperties("x")``) with no key under them in the
  service's configuration files (see ``unbound_section_sites``).

Only the line and the kind of each site are recorded, never the code.
The scanner also follows file-local helpers through syntax trees (see
``syntax_wrappers``) and drops the sites those helpers explain.
"""

import os
import re
from typing import Final

from pydantic import BaseModel, Field

from src.core.models import ConfigFramework, OpaqueReferenceKind, OpaqueReferenceSite

_JAVASCRIPT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".cjs", ".js", ".jsx", ".mjs", ".svelte", ".ts", ".tsx", ".vue"}
)
_COMMENT_PREFIXES: Final[tuple[str, ...]] = ("//", "#", "*", "/*")
# Section names that configure the service's own listener (as config_readers).
_DOTNET_LISTEN_SECTIONS: Final[frozenset[str]] = frozenset(
    {"kestrel", "urls", "http_ports", "https_ports", "allowedhosts"}
)

# Calls whose first argument names a configuration key or environment
# variable, by file suffix: (pattern ending at the opening delimiter, kind).
_CS_KEY_CALLS: Final[tuple[tuple[re.Pattern[str], OpaqueReferenceKind], ...]] = (
    (re.compile(r"\b\w*[Cc]onfig\w*\s*\["), OpaqueReferenceKind.DYNAMIC_CONFIG_KEY),
    (
        re.compile(r"\.(?:GetValue<[^>()]+>|GetConnectionString)\("),
        OpaqueReferenceKind.DYNAMIC_CONFIG_KEY,
    ),
    # Section binds: ``nameof(Options)`` is a literal section (see _CS_SECTIONS).
    (
        re.compile(r"\.(?:GetSection|GetRequiredSection|BindConfiguration)\((?!\s*nameof\()"),
        OpaqueReferenceKind.DYNAMIC_CONFIG_KEY,
    ),
    # .NET Aspire client integrations read ConnectionStrings:<name>; a name
    # that isn't a literal hides the connection (a lambda is an options
    # callback, as EF Core's AddDbContext<T>(o => ...), not a name).
    (
        re.compile(
            r"\.Add(?:Keyed)?(?!Http)[A-Z]\w*?(?:Client|DbContext|DataSource|Producer|Consumer|"
            r"DistributedCache|OutputCache)(?:<[^>()]*>)?\((?![^,)]*=>)"
        ),
        OpaqueReferenceKind.DYNAMIC_CONFIG_KEY,
    ),
    (re.compile(r"\bEnvironment\.GetEnvironmentVariable\("), OpaqueReferenceKind.DYNAMIC_ENV_NAME),
)
_JAVA_KEY_CALLS: Final[tuple[tuple[re.Pattern[str], OpaqueReferenceKind], ...]] = (
    (re.compile(r"\.getProperty\("), OpaqueReferenceKind.DYNAMIC_CONFIG_KEY),
    (re.compile(r"\bSystem\.getenv\((?!\s*\))"), OpaqueReferenceKind.DYNAMIC_ENV_NAME),
    (re.compile(r"\bSystem\.getenv\(\s*\)\.get\("), OpaqueReferenceKind.DYNAMIC_ENV_NAME),
)
_PYTHON_KEY_CALLS: Final[tuple[tuple[re.Pattern[str], OpaqueReferenceKind], ...]] = (
    (re.compile(r"(?<![\w$.])(?:os\.)?getenv\("), OpaqueReferenceKind.DYNAMIC_ENV_NAME),
    (re.compile(r"(?<![\w$.])(?:os\.)?environ(?:\.get\(|\[)"), OpaqueReferenceKind.DYNAMIC_ENV_NAME),
)
_JAVASCRIPT_KEY_CALLS: Final[tuple[tuple[re.Pattern[str], OpaqueReferenceKind], ...]] = (
    (re.compile(r"\b(?:process|import\.meta)\.env\["), OpaqueReferenceKind.DYNAMIC_ENV_NAME),
)
_GO_KEY_CALLS: Final[tuple[tuple[re.Pattern[str], OpaqueReferenceKind], ...]] = (
    (re.compile(r"\bos\.(?:Getenv|LookupEnv)\("), OpaqueReferenceKind.DYNAMIC_ENV_NAME),
)
_RUBY_KEY_CALLS: Final[tuple[tuple[re.Pattern[str], OpaqueReferenceKind], ...]] = (
    (re.compile(r"(?<![\w.])ENV(?:\.fetch\(|\[)"), OpaqueReferenceKind.DYNAMIC_ENV_NAME),
)

# Client address sinks: the pattern ends where the address expression starts.
_CS_ADDRESS_CALLS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bGrpcChannel\.ForAddress\("),
    re.compile(r"\bAddress\s*=\s*new\s*(?:Uri)?\("),
)
_CS_ADDRESS_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(r"\bBaseAddress\s*=\s*(?!=)")
_JAVA_ADDRESS_CALLS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\.baseUrl\("),
    re.compile(r"\b(?:WebClient|RestClient)\.create\((?!\s*\))"),
    re.compile(r"\.rootUri\("),
    re.compile(r"\bManagedChannelBuilder\.(?:forTarget|forAddress)\("),
)

# Address expressions the scanner follows.
_LITERAL: Final[re.Pattern[str]] = re.compile(r"""^(?:@?"|'|`(?![^`]*\$\{)|r"|"{3})""")
_INTERPOLATED_LITERAL_HOST: Final[re.Pattern[str]] = re.compile(
    r"""^(?:\$@?|@\$)"[a-z][a-z0-9+.-]*://[^{"/]+[/:"]""", re.IGNORECASE
)
_CS_CONFIG_READ: Final[re.Pattern[str]] = re.compile(
    r"""^(?:[\w.]*\w*[Cc]onfig\w*\s*\[\s*@?"|[\w.()]*\.(?:GetValue<[^>()]+>|GetConnectionString)\(\s*@?"|"""
    r"""Environment\.GetEnvironmentVariable\(\s*")"""
)
_JAVA_CONFIG_READ: Final[re.Pattern[str]] = re.compile(
    r"""^(?:[\w.()]*\.get(?:Required)?Property\(\s*"|System\.getenv\(\s*")"""
)
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^(?:this\.)?(?P<name>[A-Za-z_]\w*)$")
# ``new Uri(...)`` or C# 9 target-typed ``new(...)``.
_NEW_URI: Final[re.Pattern[str]] = re.compile(r"^new\s*(?:Uri)?\(")

_CS_SECTIONS: Final[re.Pattern[str]] = re.compile(
    r"""\.(?:GetSection|GetRequiredSection|BindConfiguration)\(\s*"""
    r"""(?:@?"(?P<section>[^"]+)"|nameof\(\s*(?:[\w.]+\.)?(?P<nameof>\w+)\s*\))"""
)
_SPRING_SECTIONS: Final[re.Pattern[str]] = re.compile(
    r"""@ConfigurationProperties\(\s*(?:(?:prefix|value)\s*=\s*)?"(?P<section>[^"]+)\""""
)


class BoundSection(BaseModel):
    """A configuration section code binds as a whole."""

    section: str = Field(min_length=1)
    framework: ConfigFramework
    line: int = Field(ge=1)


def _line(content: str, position: int) -> int:
    return content.count("\n", 0, position) + 1


def _in_comment(content: str, position: int) -> bool:
    start = content.rfind("\n", 0, position) + 1
    return content[start:position].lstrip().startswith(_COMMENT_PREFIXES)


def _argument(content: str, start: int, stop: str = ",)]") -> str:
    """The expression starting at ``start``, up to a top-level ``stop`` char."""
    depth = 0
    quote: str | None = None
    position = start
    limit = min(len(content), start + 2000)
    while position < limit:
        char = content[position]
        if quote:
            if char == "\\":
                position += 1
            elif char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and char in stop:
            break
        position += 1
    return content[start:position].strip()


def _key_calls(suffix: str) -> tuple[tuple[re.Pattern[str], OpaqueReferenceKind], ...]:
    if suffix == ".cs":
        return _CS_KEY_CALLS
    if suffix == ".java":
        return _JAVA_KEY_CALLS
    if suffix in (".py", ".php"):
        return _PYTHON_KEY_CALLS
    if suffix in _JAVASCRIPT_SUFFIXES:
        return _JAVASCRIPT_KEY_CALLS
    if suffix == ".go":
        return _GO_KEY_CALLS
    if suffix == ".rb":
        return _RUBY_KEY_CALLS
    return ()


# A string literal (no escapes across quotes), captured.
_STRING_LITERAL: Final[str] = r"""(?:"([^"\\\n]*)"|'([^'\\\n]*)')"""
_LITERAL_LIST: Final[re.Pattern[str]] = re.compile(
    rf"^\s*(?:{_STRING_LITERAL}\s*,\s*)*(?:{_STRING_LITERAL}\s*,?\s*)?$"
)
# Loops binding ``{var}`` to each element of a collection named ``coll``.
_LOOP_HEADERS: Final[tuple[str, ...]] = (
    r"\bfor\s*\(\s*(?:final\s+)?[\w<>\[\]?]+\s+{var}\s*:\s*(?P<coll>[\w.]+)\s*\)",  # Java
    r"\bforeach\s*\(\s*[\w<>\[\]?]+\s+{var}\s+in\s+(?P<coll>[\w.]+)\s*\)",  # C#
    r"\bfor\s+{var}\s+in\s+(?P<coll>[\w.]+)\s*:",  # Python
    r"\bfor\s+\w+\s*,\s*{var}\s*:=\s*range\s+(?P<coll>[\w.]+)",  # Go
    r"\bfor\s*\(\s*(?:const|let|var)\s+{var}\s+of\s+(?P<coll>[\w.]+)\s*\)",  # JavaScript
    r"(?P<coll>[\w.]+)\.forEach\(\s*(?:function\s*)?\(?\s*{var}\b",  # JavaScript
    r"\bforeach\s*\(\s*\$?(?P<coll>[\w.]+)\s+as\s+\${var}\s*\)",  # PHP
)
# Function declarations: (pattern, group holding the modifiers or None).
_FUNCTION_DECLARATIONS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?P<mods>(?:\b(?:private|public|protected|internal|static|final|function)\s+)*)"
        r"[\w<>\[\]?]*\s+(?P<fn>\w+)\s*\((?P<params>[^()]*)\)\s*(?:throws[^{;]*)?\{"
    ),
    re.compile(r"\bdef\s+(?P<fn>\w+)\s*\((?P<params>[^()]*)\)"),
    re.compile(r"\bfunc\s+(?:\([^()]*\)\s*)?(?P<fn>\w+)\s*\((?P<params>[^()]*)\)"),
    re.compile(r"\bfunction\s+(?P<fn>\w+)\s*\((?P<params>[^()]*)\)"),
    re.compile(r"\b(?:const|let|var)\s+(?P<fn>\w+)\s*=\s*\((?P<params>[^()]*)\)\s*=>"),
)


class FollowedEnvRead(BaseModel):
    """An environment read through a name the same file fixes to a literal.

    The read itself takes a variable, but every value that variable can
    take is a literal in the file: a loop over a literal list of names, or
    a file-local wrapper whose every call passes a literal name.
    ``default`` is the literal a wrapper call passes as its fallback.
    """

    name: str = Field(min_length=1)
    default: str | None = None


def _literal_list(content: str, collection: str) -> list[str] | None:
    """The string literals a collection is initialized with, if it holds only literals."""
    name = re.escape(collection.rsplit(".", 1)[-1])
    for match in re.finditer(rf"\b{name}\b[^=;\n(){{}}]*=\s*", content):
        rest = content[match.end(): match.end() + 4000]
        opening = re.match(r"(?:new\s+[\w<>\[\]]+\s*|\[\]\w+\s*|(?:List|Set|Arrays)\.(?:of|asList)\s*|array\s*)?([\[{(])", rest)
        if opening is None:
            continue
        body = _argument(rest, opening.end(), "")
        literal = _LITERAL_LIST.match(body)
        if literal is None:
            return None
        return [a or b for a, b in re.findall(_STRING_LITERAL, body)]
    return None


def _loop_names(content: str, variable: str, position: int) -> list[str] | None:
    """Names a loop variable takes when the nearest loop over it iterates a literal list."""
    nearest: re.Match[str] | None = None
    for header in _LOOP_HEADERS:
        for match in re.finditer(header.format(var=re.escape(variable)), content[:position]):
            if nearest is None or match.start() > nearest.start():
                nearest = match
    return _literal_list(content, nearest.group("coll")) if nearest else None


def _file_local(declaration: re.Match[str], content: str, suffix: str) -> bool:
    """Whether a function is visibly callable only from its own file."""
    name = declaration.group("fn")
    modifiers = declaration.groupdict().get("mods") or ""
    if suffix in (".java", ".cs", ".php"):
        return "private" in modifiers
    if suffix == ".go":
        return name[:1].islower()
    if suffix in _JAVASCRIPT_SUFFIXES:
        return not re.search(rf"\bexport\b[^\n]*\b{name}\b|\bexports\.{name}\b|module\.exports\b[^;]*\b{name}\b", content)
    return name.startswith("_")


def _wrapper_calls(content: str, variable: str, position: int, suffix: str) -> list[FollowedEnvRead] | None:
    """Literal calls of the file-local function whose parameter ``variable`` is read."""
    nearest: re.Match[str] | None = None
    for pattern in _FUNCTION_DECLARATIONS:
        for match in pattern.finditer(content[:position]):
            if re.search(rf"\b{re.escape(variable)}\b", match.group("params")) and (
                nearest is None or match.start() > nearest.start()
            ):
                nearest = match
    if nearest is None or not _file_local(nearest, content, suffix):
        return None
    reads: list[FollowedEnvRead] = []
    # Unqualified (or this./self.) calls: System.getenv( is not the wrapper.
    for call in re.finditer(rf"(?<![\w$.>])(?:this\.|self\.|\$this->)?{re.escape(nearest.group('fn'))}\s*\(", content):
        if nearest.start("fn") <= call.start() < nearest.end() or _in_comment(content, call.start()):
            continue
        arguments = _argument(content, call.end(), "")
        literals = re.match(rf"^\s*{_STRING_LITERAL}\s*(?:,\s*(?:{_STRING_LITERAL})?)?", arguments)
        if literals is None:
            return None
        first, second = literals.group(1) or literals.group(2), literals.group(3) or literals.group(4)
        reads.append(FollowedEnvRead(name=first, default=second))
    return reads or None


def _followed(content: str, argument: str, position: int, suffix: str) -> list[FollowedEnvRead] | None:
    """The literal names a dynamic environment read takes, or None if not all are known."""
    identifier = _IDENTIFIER.match(argument.lstrip("$"))
    if identifier is None:
        return None
    variable = identifier.group("name")
    names = _loop_names(content, variable, position)
    if names is not None:
        return [FollowedEnvRead(name=name) for name in names]
    return _wrapper_calls(content, variable, position, suffix)


def followed_env_reads(content: str, path: str) -> list[FollowedEnvRead]:
    """Environment reads through a variable whose every value is a literal
    in the same file (see ``FollowedEnvRead``), in order of appearance."""
    suffix = os.path.splitext(path)[1].lower()
    reads: list[FollowedEnvRead] = []
    for pattern, kind in _key_calls(suffix):
        if kind is not OpaqueReferenceKind.DYNAMIC_ENV_NAME:
            continue
        for match in pattern.finditer(content):
            argument = _argument(content, match.end())
            if argument and not _LITERAL.match(argument) and not _in_comment(content, match.start()):
                reads.extend(_followed(content, argument, match.start(), suffix) or [])
    return list({(read.name, read.default): read for read in reads}.values())


def _dynamic_key_sites(content: str, suffix: str) -> list[OpaqueReferenceSite]:
    sites: list[OpaqueReferenceSite] = []
    for pattern, kind in _key_calls(suffix):
        for match in pattern.finditer(content):
            if _in_comment(content, match.start()):
                continue
            argument = _argument(content, match.end())
            if not argument or _LITERAL.match(argument):
                continue
            if kind is OpaqueReferenceKind.DYNAMIC_ENV_NAME and _followed(content, argument, match.start(), suffix):
                continue
            sites.append(OpaqueReferenceSite(line=_line(content, match.start()), kind=kind))
    return sites


def _understood_read(expression: str, suffix: str) -> bool:
    if _LITERAL.match(expression) or _INTERPOLATED_LITERAL_HOST.match(expression):
        return True
    config_read = _CS_CONFIG_READ if suffix == ".cs" else _JAVA_CONFIG_READ
    return bool(config_read.match(expression))


def _assigned_from_understood(content: str, name: str, suffix: str, depth: int) -> bool:
    """Whether the file assigns ``name`` from something the scanner follows."""
    if suffix == ".java" and re.search(
        rf"""@Value\(\s*"[^"]*"\s*\)\s*(?:(?:private|protected|public|final|static)\s+)*"""
        rf"""[\w<>\[\],.? ]+?\s+{re.escape(name)}\b""",
        content,
    ):
        return True
    for match in re.finditer(rf"\b{re.escape(name)}\s*=(?!=)\s*", content):
        if _understood(_argument(content, match.end(), ",);"), content, suffix, depth + 1):
            return True
    return False


def _understood(expression: str, content: str, suffix: str, depth: int = 0) -> bool:
    """Whether an address expression is a literal, a keyed read, or assigned from one."""
    expression = expression.strip()
    if _NEW_URI.match(expression):
        expression = _argument(expression, expression.index("(") + 1)
    if _understood_read(expression, suffix):
        return True
    identifier = _IDENTIFIER.match(expression)
    if identifier is None or depth >= 2:
        return False
    return _assigned_from_understood(content, identifier.group("name"), suffix, depth)


def _client_address_sites(content: str, suffix: str) -> list[OpaqueReferenceSite]:
    sinks: list[tuple[re.Match[str], str]] = []
    if suffix == ".cs":
        sinks.extend((match, ",);") for pattern in _CS_ADDRESS_CALLS for match in pattern.finditer(content))
        sinks.extend((match, ",;") for match in _CS_ADDRESS_ASSIGNMENT.finditer(content))
    elif suffix == ".java":
        sinks.extend((match, ",)") for pattern in _JAVA_ADDRESS_CALLS for match in pattern.finditer(content))
    sites: list[OpaqueReferenceSite] = []
    for match, stop in sinks:
        if _in_comment(content, match.start()):
            continue
        argument = _argument(content, match.end(), stop)
        if argument and not _understood(argument, content, suffix):
            sites.append(
                OpaqueReferenceSite(
                    line=_line(content, match.start()),
                    kind=OpaqueReferenceKind.CLIENT_ADDRESS_EXPRESSION,
                )
            )
    return sites


def opaque_reference_sites(content: str, path: str) -> list[OpaqueReferenceSite]:
    """References in one code file the scanner reads but can't follow.

    Sorted by line, one per (line, kind).
    """
    suffix = os.path.splitext(path)[1].lower()
    sites = [*_dynamic_key_sites(content, suffix), *_client_address_sites(content, suffix)]
    unique = {(site.line, site.kind): site for site in sites}
    return [unique[key] for key in sorted(unique, key=lambda k: (k[0], k[1].value))]


def bound_sections(content: str, path: str) -> list[BoundSection]:
    """Configuration sections one code file binds as a whole."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".cs":
        pattern, framework = _CS_SECTIONS, ConfigFramework.DOTNET
    elif suffix == ".java":
        pattern, framework = _SPRING_SECTIONS, ConfigFramework.SPRING
    else:
        return []
    sections: list[BoundSection] = []
    for match in pattern.finditer(content):
        section = (match.group("section") or match.groupdict().get("nameof") or "").strip()
        if not section or _in_comment(content, match.start()):
            continue
        if framework is ConfigFramework.DOTNET and section.split(":", 1)[0].lower() in _DOTNET_LISTEN_SECTIONS:
            continue
        sections.append(BoundSection(section=section, framework=framework, line=_line(content, match.start())))
    return sections


def _relaxed(name: str) -> str:
    """Spring's relaxed form: lower case without dashes or underscores."""
    return re.sub(r"[-_]", "", name.lower())


def _under(key: str, section: str, separator: str) -> bool:
    """Whether ``key`` lies under ``section`` at any depth: a section read
    from another section (``identity.GetSection("Scopes")``) is relative,
    so ``Identity:Scopes:read`` is under ``Scopes``."""
    wrapped = separator + key + separator
    return separator + section + separator in wrapped.replace("[", separator).replace("]", "")


def section_has_keys(section: BoundSection, keys: dict[ConfigFramework, set[str]]) -> bool:
    """Whether any configuration file key lies under ``section``."""
    if section.framework is ConfigFramework.DOTNET:
        prefix = section.section.lower()
        return any(_under(key.lower(), prefix, ":") for key in keys.get(section.framework, set()))
    prefix = _relaxed(section.section)
    return any(_under(_relaxed(key), prefix, ".") for key in keys.get(section.framework, set()))


def unbound_section_sites(
    sections: list[BoundSection], keys: dict[ConfigFramework, set[str]]
) -> list[OpaqueReferenceSite]:
    """Sites for bound sections with no key in the service's configuration files."""
    return [
        OpaqueReferenceSite(
            line=section.line, kind=OpaqueReferenceKind.UNBOUND_SECTION, detail=section.section
        )
        for section in sections
        if not section_has_keys(section, keys)
    ]


# Spring Cloud Config: ``spring.config.import: configserver:http://...``
# (YAML or properties, any profile document) or the properties form
# ``spring.cloud.config.uri=...``.
_SPRING_EXTERNAL_CONFIG: Final[re.Pattern[str]] = re.compile(
    r"(?P<import>configserver:)|^\s*(?P<uri>spring\.cloud\.config\.uri)\s*[:=]", re.IGNORECASE
)


def external_config_sites(content: str) -> list[OpaqueReferenceSite]:
    """Sites in a Spring configuration file that import configuration from
    a config server: keys served there are unknown, so dependencies they
    configure (a Eureka zone, a datasource) are invisible."""
    sites: list[OpaqueReferenceSite] = []
    for number, line in enumerate(content.splitlines(), start=1):
        if line.lstrip().startswith(("#", "!")):
            continue
        match = _SPRING_EXTERNAL_CONFIG.search(line)
        if match:
            detail = "spring.config.import" if match.group("import") else "spring.cloud.config.uri"
            sites.append(
                OpaqueReferenceSite(line=number, kind=OpaqueReferenceKind.EXTERNAL_CONFIG_SOURCE, detail=detail)
            )
    return sites

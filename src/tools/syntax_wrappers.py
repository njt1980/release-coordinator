"""Follow file-local wrapper functions one hop, using syntax trees.

A service often reads its configuration through a small helper::

    private string Url(string key, string fallback) => _configuration[key] ?? fallback;
    ...
    var basket = Url("Services:Basket", "http://basket-api");

The read inside the helper takes a parameter, so the regex scanner sees a
dynamic key (``REFERENCES_NOT_UNDERSTOOD``). When the helper is visibly
callable only from its own file and every call passes a literal, the key
is known after all. This module parses the file with tree-sitter (C#, Java,
JavaScript/TypeScript, Python), finds such helpers and their calls, and
reports:

- the environment names and configuration keys the helper reads, with the
  literal default a call passes, when one does (``FollowedRead``);
- the lines of the reads (and client address sinks) inside helpers that are
  fully followed, so the caller can drop their opaque sites.

A client address sink (``BaseAddress = new Uri(p)``, ``GrpcChannel.ForAddress(p)``,
``WebClient.create(p)``...) inside a helper is followed when every call passes
a literal or an environment/configuration read with a literal key: those
arguments are read by the scanner already.

One hop only: a helper calling another helper is not followed. A helper is
not followed when its name is declared twice in the file, when it is used
other than by a direct call (passed as a delegate), or when any call passes
something other than a literal. A file that doesn't parse cleanly, a
language without a grammar, or a missing tree-sitter install yields an
empty result: the regex scanner's findings then stand alone.

Only names, keys, defaults and line numbers leave this module, never code.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from tree_sitter import Node, Parser


class SyntaxLanguage(str, Enum):
    """Languages this module parses."""

    CSHARP = "csharp"
    JAVA = "java"
    TYPESCRIPT = "typescript"
    TSX = "tsx"
    PYTHON = "python"


class WrapperSinkKind(str, Enum):
    """What a helper does with a parameter.

    ENV_NAME: reads the environment variable it names.
    CONFIG_KEY: reads the configuration key it names (C# IConfiguration,
        Spring ``getProperty``).
    CLIENT_ADDRESS: uses it as an HTTP/gRPC client address.
    """

    ENV_NAME = "env_name"
    CONFIG_KEY = "config_key"
    CLIENT_ADDRESS = "client_address"


class FollowedRead(BaseModel):
    """A read inside a file-local helper, with the literal a call passes.

    ``name`` is the environment variable or configuration key; a C#
    ``GetConnectionString`` read is the key ``ConnectionStrings:<name>``.
    ``default`` is the literal fallback a call passes or the helper falls
    back to (``?? "http://..."``).
    """

    kind: WrapperSinkKind
    name: str = Field(min_length=1)
    default: str | None = None
    language: SyntaxLanguage


class FollowedSink(BaseModel):
    """A line holding a sink whose every value is known (see module doc)."""

    kind: WrapperSinkKind
    line: int = Field(ge=1)


class WrapperAnalysis(BaseModel):
    """What one file's helpers read. ``parsed`` is False when the file was
    not analysed (no grammar, parse errors, tree-sitter missing)."""

    parsed: bool = False
    reads: list[FollowedRead] = Field(default_factory=list)
    followed_sinks: list[FollowedSink] = Field(default_factory=list)


_SUFFIX_LANGUAGES: Final[dict[str, SyntaxLanguage]] = {
    ".cs": SyntaxLanguage.CSHARP,
    ".java": SyntaxLanguage.JAVA,
    ".ts": SyntaxLanguage.TYPESCRIPT,
    ".mts": SyntaxLanguage.TYPESCRIPT,
    ".cts": SyntaxLanguage.TYPESCRIPT,
    ".tsx": SyntaxLanguage.TSX,
    ".js": SyntaxLanguage.TSX,
    ".jsx": SyntaxLanguage.TSX,
    ".mjs": SyntaxLanguage.TSX,
    ".cjs": SyntaxLanguage.TSX,
    ".py": SyntaxLanguage.PYTHON,
}
# Files beyond this size are left to the regex scanner.
_MAX_BYTES: Final[int] = 512_000

_CALLS: Final[frozenset[str]] = frozenset(
    {"invocation_expression", "method_invocation", "call", "call_expression"}
)
_SUBSCRIPTS: Final[frozenset[str]] = frozenset(
    {"element_access_expression", "subscript", "subscript_expression"}
)
_CREATIONS: Final[frozenset[str]] = frozenset(
    {"object_creation_expression", "implicit_object_creation_expression", "new_expression"}
)
_ASSIGNMENTS: Final[frozenset[str]] = frozenset({"assignment_expression", "assignment"})
_FALLBACKS: Final[frozenset[str]] = frozenset({"binary_expression", "boolean_operator"})
_FALLBACK_OPERATORS: Final[frozenset[str]] = frozenset({"??", "||", "or"})
_STRING_PARTS: Final[frozenset[str]] = frozenset(
    {"string_literal_content", "string_fragment", "string_content", "escape_sequence"}
)
_STRINGS: Final[frozenset[str]] = frozenset(
    {"string_literal", "verbatim_string_literal", "raw_string_literal", "string", "template_string"}
)
_NULLS: Final[frozenset[str]] = frozenset({"null_literal", "none", "null", "undefined"})
_INTERPOLATIONS: Final[frozenset[str]] = frozenset(
    {"interpolation", "template_substitution", "string_literal_encoding"}
)

# Sinks, as patterns over the callee (or subscripted object) text with
# whitespace removed: (pattern, kind, languages).
_CALL_SINKS: Final[tuple[tuple[re.Pattern[str], WrapperSinkKind, frozenset[SyntaxLanguage]], ...]] = (
    (
        re.compile(r"^(?:System\.)?Environment\.GetEnvironmentVariable$"),
        WrapperSinkKind.ENV_NAME,
        frozenset({SyntaxLanguage.CSHARP}),
    ),
    (
        re.compile(r"\.(?:GetValue<[^>()]+>|GetConnectionString)$"),
        WrapperSinkKind.CONFIG_KEY,
        frozenset({SyntaxLanguage.CSHARP}),
    ),
    (
        re.compile(r"^(?:System\.getenv|System\.getenv\(\)\.get)$"),
        WrapperSinkKind.ENV_NAME,
        frozenset({SyntaxLanguage.JAVA}),
    ),
    (
        re.compile(r"\.get(?:Required)?Property$"),
        WrapperSinkKind.CONFIG_KEY,
        frozenset({SyntaxLanguage.JAVA}),
    ),
    (
        re.compile(r"^(?:os\.)?(?:getenv|environ\.get)$"),
        WrapperSinkKind.ENV_NAME,
        frozenset({SyntaxLanguage.PYTHON}),
    ),
    (
        re.compile(r"^(?:Grpc\.Net\.Client\.)?GrpcChannel\.ForAddress$"),
        WrapperSinkKind.CLIENT_ADDRESS,
        frozenset({SyntaxLanguage.CSHARP}),
    ),
    (
        re.compile(
            r"(?:\.baseUrl|^(?:WebClient|RestClient)\.create|\.rootUri|"
            r"^ManagedChannelBuilder\.(?:forTarget|forAddress))$"
        ),
        WrapperSinkKind.CLIENT_ADDRESS,
        frozenset({SyntaxLanguage.JAVA}),
    ),
)
_SUBSCRIPT_SINKS: Final[tuple[tuple[re.Pattern[str], WrapperSinkKind, frozenset[SyntaxLanguage]], ...]] = (
    (
        re.compile(r"(?:^|\.)\w*[Cc]onfig\w*$"),
        WrapperSinkKind.CONFIG_KEY,
        frozenset({SyntaxLanguage.CSHARP}),
    ),
    (re.compile(r"^(?:os\.)?environ$"), WrapperSinkKind.ENV_NAME, frozenset({SyntaxLanguage.PYTHON})),
    (
        re.compile(r"^(?:process|import\.meta)\.env$"),
        WrapperSinkKind.ENV_NAME,
        frozenset({SyntaxLanguage.TYPESCRIPT, SyntaxLanguage.TSX}),
    ),
)
# C# address properties: ``BaseAddress = ...``, ``Address = new Uri(...)``.
_CS_ADDRESS_TARGET: Final[re.Pattern[str]] = re.compile(r"(?:^|\.)(?:BaseAddress|Address)$")
_URI_TYPE: Final[re.Pattern[str]] = re.compile(r"^(?:System\.)?Uri$")
_JS_EXPORTS: Final[str] = r"\bexports\.{fn}\b|module\.exports\b[^;]*\b{fn}\b"


@lru_cache(maxsize=None)
def _parser(language: SyntaxLanguage) -> Parser | None:
    """A parser for ``language``, or None when tree-sitter isn't installed."""
    try:
        from tree_sitter import Language, Parser

        capsule: object
        if language is SyntaxLanguage.CSHARP:
            import tree_sitter_c_sharp as grammar_cs

            capsule = grammar_cs.language()
        elif language is SyntaxLanguage.JAVA:
            import tree_sitter_java as grammar_java

            capsule = grammar_java.language()
        elif language is SyntaxLanguage.PYTHON:
            import tree_sitter_python as grammar_python

            capsule = grammar_python.language()
        else:
            import tree_sitter_typescript as grammar_ts

            capsule = (
                grammar_ts.language_typescript()
                if language is SyntaxLanguage.TYPESCRIPT
                else grammar_ts.language_tsx()
            )
        return Parser(Language(capsule))
    except (ImportError, ValueError, OSError):
        return None


def syntax_language(path: str) -> SyntaxLanguage | None:
    """The grammar a file is parsed with, by suffix."""
    return _SUFFIX_LANGUAGES.get(os.path.splitext(path)[1].lower())


def parse(content: str, path: str) -> Node | None:
    """The syntax tree's root, or None when the file can't be analysed."""
    language = syntax_language(path)
    source = content.encode("utf-8", errors="replace")
    if language is None or len(source) > _MAX_BYTES:
        return None
    parser = _parser(language)
    if parser is None:
        return None
    root = parser.parse(source).root_node
    return None if root.has_error else root


# --- syntax helpers ---------------------------------------------------------


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None and node.text else ""


def _compact(node: Node | None) -> str:
    return re.sub(r"\s+", "", _text(node))


def _walk(node: Node) -> list[Node]:
    """All named descendants of ``node``, itself included, in source order."""
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        found.append(current)
        stack.extend(reversed(current.named_children))
    return found


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _expression(argument: Node) -> Node:
    """The expression an argument node holds (C# wraps it in ``argument``)."""
    if argument.type == "argument" and argument.named_child_count:
        return argument.named_children[-1]
    return argument


def _arguments(call: Node) -> list[Node]:
    """A call's or creation's arguments, without comments."""
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return []
    return [child for child in arguments.named_children if child.type != "comment"]


def _callee(call: Node) -> str:
    """The callee text of a call (``System.getenv``, ``os.environ.get``), compacted."""
    arguments = call.child_by_field_name("arguments")
    if arguments is None or call.text is None:
        return ""
    prefix = call.text[: arguments.start_byte - call.start_byte].decode("utf-8", errors="replace")
    return re.sub(r"\s+", "", prefix)


def _subscript_parts(node: Node) -> tuple[Node | None, Node | None]:
    """(object, key) of a subscript expression."""
    if node.type == "element_access_expression":
        subscript = node.child_by_field_name("subscript")
        keys = [_expression(child) for child in subscript.named_children] if subscript else []
        return node.child_by_field_name("expression"), keys[0] if len(keys) == 1 else None
    if node.type == "subscript":
        return node.child_by_field_name("value"), node.child_by_field_name("subscript")
    return node.child_by_field_name("object"), node.child_by_field_name("index")


def literal(node: Node | None) -> str | None:
    """The value of a plain string literal, or None for anything else."""
    if node is None:
        return None
    node = _expression(node)
    if node.type not in _STRINGS:
        return None
    if any(child.type in _INTERPOLATIONS for child in node.named_children):
        return None
    if node.type == "string" and re.match(r"^[A-Za-z]*[fF]", _text(node)):
        return None  # Python f-string
    parts = [child for child in node.named_children if child.type in _STRING_PARTS]
    if parts:
        return "".join(_text(part) if part.type != "escape_sequence" else "" for part in parts)
    return re.sub(r"""^[@$rRbBuU]*(?:\"{3}|'{3}|["'`])|(?:\"{3}|'{3}|["'`])$""", "", _text(node))


def _is_null(node: Node | None) -> bool:
    return node is not None and _expression(node).type in _NULLS


# --- sinks ------------------------------------------------------------------


@dataclass(frozen=True)
class _Sink:
    """A read or address use whose value is ``value`` (the node passed in)."""

    kind: WrapperSinkKind
    value: Node
    line: int
    node_id: int
    default: Node | None = None
    key_prefix: str = ""


def _call_sink(node: Node, language: SyntaxLanguage) -> _Sink | None:
    callee = _callee(node)
    for pattern, kind, languages in _CALL_SINKS:
        if language in languages and pattern.search(callee):
            arguments = [_expression(argument) for argument in _arguments(node)]
            if not arguments:
                return None
            default = arguments[1] if len(arguments) > 1 else None
            prefix = "ConnectionStrings:" if callee.endswith(".GetConnectionString") else ""
            return _Sink(
                kind=kind, value=arguments[0], line=_line(node), node_id=node.id, default=default, key_prefix=prefix
            )
    return None


def _subscript_sink(node: Node, language: SyntaxLanguage) -> _Sink | None:
    target, key = _subscript_parts(node)
    if key is None:
        return None
    for pattern, kind, languages in _SUBSCRIPT_SINKS:
        if language in languages and pattern.search(_compact(target)):
            return _Sink(kind=kind, value=key, line=_line(node), node_id=node.id)
    return None


def _address_value(node: Node) -> Node:
    """``new Uri(x)`` and ``new(x)`` hold address ``x``."""
    node = _expression(node)
    if node.type in _CREATIONS:
        type_node = node.child_by_field_name("type")
        arguments = _arguments(node)
        if (type_node is None or _URI_TYPE.match(_compact(type_node))) and len(arguments) == 1:
            return _expression(arguments[0])
    return node


def _assignment_sink(node: Node, language: SyntaxLanguage) -> _Sink | None:
    if language is not SyntaxLanguage.CSHARP:
        return None
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    if left is None or right is None or not _CS_ADDRESS_TARGET.search(_compact(left)):
        return None
    return _Sink(
        kind=WrapperSinkKind.CLIENT_ADDRESS, value=_address_value(right), line=_line(node), node_id=node.id
    )


def _sink(node: Node, language: SyntaxLanguage) -> _Sink | None:
    """The sink ``node`` is, if any."""
    if node.type in _CALLS:
        sink = _call_sink(node, language)
        if sink is not None and sink.kind is WrapperSinkKind.CLIENT_ADDRESS:
            return _Sink(kind=sink.kind, value=_address_value(sink.value), line=sink.line, node_id=sink.node_id)
        return sink
    if node.type in _SUBSCRIPTS:
        return _subscript_sink(node, language)
    if node.type in _ASSIGNMENTS:
        return _assignment_sink(node, language)
    return None


def _fallback_operand(sink_node: Node) -> Node | None:
    """The other operand when a read is the left side of ``??``/``||``/``or``."""
    parent = sink_node.parent
    while parent is not None and parent.type == "parenthesized_expression":
        sink_node, parent = parent, parent.parent
    if parent is None or parent.type not in _FALLBACKS:
        return None
    operator = parent.child_by_field_name("operator")
    left, right = parent.child_by_field_name("left"), parent.child_by_field_name("right")
    if operator is None or _text(operator) not in _FALLBACK_OPERATORS or left is None:
        return None
    return right if left.id == sink_node.id else None


def _understood_address(argument: Node, language: SyntaxLanguage) -> bool:
    """A call-site address argument the scanner already reads: a literal, or
    an environment/configuration read with a literal key."""
    argument = _address_value(argument)
    if literal(argument) is not None:
        return True
    sink = _sink(argument, language) if argument.type in _CALLS | _SUBSCRIPTS else None
    return (
        sink is not None
        and sink.kind is not WrapperSinkKind.CLIENT_ADDRESS
        and literal(sink.value) is not None
    )


# --- helpers ----------------------------------------------------------------


@dataclass(frozen=True)
class _BoundSink:
    """A sink in a helper reading parameter ``index``; its default is
    parameter ``default_index`` or the literal ``fixed_default``."""

    sink: _Sink
    index: int
    default_index: int | None
    fixed_default: str | None


@dataclass
class _Helper:
    """A function declaration with its parameters and body."""

    name: str
    name_node: Node
    parameters: list[str]
    parameter_defaults: list[Node | None]
    body: Node
    local: bool
    sinks: list[_BoundSink] = field(default_factory=list)


def _parameters(node: Node | None) -> tuple[list[str], list[Node | None]]:
    """Parameter names and their declared default values, in order."""
    names: list[str] = []
    defaults: list[Node | None] = []
    if node is None:
        return names, defaults
    if node.type == "identifier":  # JavaScript ``n => ...``
        return [_text(node)], [None]
    for child in node.named_children:
        name_node = child.child_by_field_name("name") or child.child_by_field_name("pattern")
        if child.type == "identifier":
            name_node = child
        if name_node is None:
            name_node = next((c for c in child.named_children if c.type == "identifier"), None)
        if name_node is None or name_node.type != "identifier":
            names.append("")
            defaults.append(None)
            continue
        names.append(_text(name_node))
        defaults.append(child.child_by_field_name("value"))
    return names, defaults


def _modifiers(node: Node) -> set[str]:
    words: set[str] = set()
    for child in node.children:
        if child.type in ("modifier", "modifiers", "accessibility_modifier"):
            words.update(_text(child).split())
    return words


def _inside(node: Node, types: frozenset[str]) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in types:
            return True
        parent = parent.parent
    return False


_FUNCTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "function_definition",
        "function_declaration",
        "method_declaration",
        "local_function_statement",
        "method_definition",
        "arrow_function",
        "function_expression",
    }
)


def _file_local(node: Node, name: str, language: SyntaxLanguage, content: str) -> bool:
    """Whether a function is visibly callable only from its own file."""
    modifiers = _modifiers(node)
    if language is SyntaxLanguage.CSHARP:
        if node.type == "local_function_statement":
            return True
        if _inside(node, frozenset({"interface_declaration"})):
            return False
        return "private" in modifiers or not modifiers & {"public", "protected", "internal"}
    if language is SyntaxLanguage.JAVA:
        return "private" in modifiers
    if language is SyntaxLanguage.PYTHON:
        return name.startswith("_") or _inside(node, frozenset({"function_definition"}))
    if node.type == "method_definition":
        return "private" in modifiers or name.startswith("#")
    if _inside(node, frozenset({"export_statement"})):
        return False
    return not re.search(_JS_EXPORTS.format(fn=re.escape(name)), content)


def _helpers(root: Node, language: SyntaxLanguage, content: str) -> list[_Helper]:
    helpers: list[_Helper] = []
    for node in _walk(root):
        name_node: Node | None
        target: Node = node
        if node.type == "variable_declarator" and language in (SyntaxLanguage.TYPESCRIPT, SyntaxLanguage.TSX):
            value = node.child_by_field_name("value")
            if value is None or value.type not in ("arrow_function", "function_expression", "function"):
                continue
            name_node, target = node.child_by_field_name("name"), value
            if name_node is None or name_node.type != "identifier":
                continue
            local_node = node
        elif node.type in _FUNCTION_TYPES - {"arrow_function", "function_expression"}:
            name_node = node.child_by_field_name("name")
            local_node = node
        else:
            continue
        body = target.child_by_field_name("body")
        if name_node is None or body is None:
            continue
        parameter_node = target.child_by_field_name("parameters") or target.child_by_field_name("parameter")
        names, defaults = _parameters(parameter_node)
        if language is SyntaxLanguage.PYTHON and names[:1] in (["self"], ["cls"]):
            names, defaults = names[1:], defaults[1:]
        name = _text(name_node)
        helpers.append(
            _Helper(
                name=name,
                name_node=name_node,
                parameters=names,
                parameter_defaults=defaults,
                body=body,
                local=_file_local(local_node, name, language, content),
            )
        )
    return helpers


def _bind_sinks(helper: _Helper, language: SyntaxLanguage) -> None:
    """Record the sinks in the helper's body whose value is a parameter."""
    for node in _walk(helper.body):
        sink = _sink(node, language)
        if sink is None or sink.value.type != "identifier":
            continue
        name = _text(sink.value)
        if name not in helper.parameters:
            continue
        default_node = sink.default if sink.default is not None else _fallback_operand(node)
        default_name = _text(default_node) if default_node is not None and default_node.type == "identifier" else ""
        default_index = helper.parameters.index(default_name) if default_name in helper.parameters else None
        if default_index is None and default_node is None:
            default_index = _conditional_default(helper, name)
        fixed_default = literal(default_node) if default_index is None else None
        helper.sinks.append(_BoundSink(sink, helper.parameters.index(name), default_index, fixed_default))


_CONDITIONALS: Final[frozenset[str]] = frozenset({"conditional_expression", "ternary_expression"})


def _conditional_default(helper: _Helper, read_parameter: str) -> int | None:
    """The parameter a helper returns when the read is missing
    (``v == null ? def : v``): the only other parameter that is a branch
    of a conditional in the body."""
    candidates: set[str] = set()
    for node in _walk(helper.body):
        if node.type not in _CONDITIONALS:
            continue
        branches = node.named_children[1:] if node.type == "ternary_expression" else node.named_children
        for branch in branches:
            text = _text(branch)
            if branch.type == "identifier" and text in helper.parameters and text != read_parameter:
                candidates.add(text)
    return helper.parameters.index(candidates.pop()) if len(candidates) == 1 else None


def _call_name(call: Node) -> str:
    """The bare name a call invokes when it is ``f(...)``, ``this.f(...)`` or ``self.f(...)``."""
    callee = _callee(call)
    match = re.fullmatch(r"(?:this\.|self\.)?([#A-Za-z_]\w*)", callee)
    return match.group(1) if match else ""


def _call_argument(call: Node, index: int, parameter: str) -> tuple[bool, Node | None]:
    """(present, node) of the argument a call passes for a parameter."""
    arguments = _arguments(call)
    for argument in arguments:
        if argument.type == "keyword_argument" and _text(argument.child_by_field_name("name")) == parameter:
            return True, argument.child_by_field_name("value")
        name = argument.child_by_field_name("name") if argument.type == "argument" else None
        if name is not None and _text(name).rstrip(":").strip() == parameter:
            return True, _expression(argument)
    positional = [
        argument
        for argument in arguments
        if argument.type != "keyword_argument"
        and not (argument.type == "argument" and argument.child_by_field_name("name") is not None)
    ]
    if index < len(positional):
        return True, _expression(positional[index])
    return False, None


def _default_value(helper: _Helper, call: Node, index: int | None) -> str | None:
    if index is None:
        return None
    present, node = _call_argument(call, index, helper.parameters[index])
    if not present:
        node = helper.parameter_defaults[index]
    return literal(node)


def _follow(helper: _Helper, calls: list[Node], language: SyntaxLanguage) -> tuple[list[FollowedRead], set[int]]:
    """Reads through the helper's sinks, and the ids of the sinks fully followed."""
    reads: list[FollowedRead] = []
    followed_ids: set[int] = set()
    for bound in helper.sinks:
        sink, index = bound.sink, bound.index
        values: list[FollowedRead] = []
        followed = True
        for call in calls:
            present, argument = _call_argument(call, index, helper.parameters[index])
            if not present or argument is None:
                followed = False
                break
            if sink.kind is WrapperSinkKind.CLIENT_ADDRESS:
                followed = _understood_address(argument, language)
                if not followed:
                    break
                continue
            value = literal(argument)
            if not value:
                followed = False
                break
            values.append(
                FollowedRead(
                    kind=sink.kind,
                    name=sink.key_prefix + value,
                    default=_default_value(helper, call, bound.default_index) or bound.fixed_default,
                    language=language,
                )
            )
        if followed:
            reads.extend(values)
            followed_ids.add(sink.node_id)
    return reads, followed_ids


def _this_member(node: Node) -> bool:
    """Whether a JavaScript property name is read off ``this`` (a method use)."""
    parent = node.parent
    target = parent.child_by_field_name("object") if parent is not None else None
    return target is not None and target.type == "this"


# Member names of a qualified access, by node type: (name field, owner field).
_MEMBER_FIELDS: Final[dict[str, tuple[str, str]]] = {
    "method_invocation": ("name", "object"),
    "field_access": ("field", "object"),
    "member_access_expression": ("name", "expression"),
    "attribute": ("attribute", "object"),
}


def _foreign_member(node: Node) -> bool:
    """Whether an identifier names a member of something other than
    ``this``/``self`` (``System.getenv``): not a use of a local helper."""
    parent = node.parent
    if parent is None or parent.type not in _MEMBER_FIELDS:
        return False
    name_field, owner_field = _MEMBER_FIELDS[parent.type]
    name, owner = parent.child_by_field_name(name_field), parent.child_by_field_name(owner_field)
    if name is None or owner is None or name.id != node.id:
        return False
    return _text(owner) not in ("this", "self")


def _resolved(sink: _Sink, language: SyntaxLanguage) -> bool:
    """Whether a sink's value is known without following a helper."""
    if sink.kind is WrapperSinkKind.CLIENT_ADDRESS:
        return _understood_address(sink.value, language)
    return literal(sink.value) is not None


def follow_wrappers(content: str, path: str) -> WrapperAnalysis:
    """Reads through file-local helpers whose every call passes literals."""
    language = syntax_language(path)
    root = parse(content, path)
    if language is None or root is None:
        return WrapperAnalysis()
    nodes = _walk(root)
    helpers = _helpers(root, language, content)
    counts: dict[str, int] = {}
    for helper in helpers:
        counts[helper.name] = counts.get(helper.name, 0) + 1
    calls_by_name: dict[str, list[Node]] = {}
    callee_ids: set[int] = set()
    for node in nodes:
        if node.type in _CALLS:
            name = _call_name(node)
            function = node.child_by_field_name("function") or node.child_by_field_name("name")
            if name and function is not None:
                calls_by_name.setdefault(name, []).append(node)
                callee_ids.update(n.id for n in _walk(function))
    identifier_uses: dict[str, list[Node]] = {}
    for node in nodes:
        if (node.type == "identifier" and not _foreign_member(node)) or (
            node.type in ("property_identifier", "private_property_identifier") and _this_member(node)
        ):
            identifier_uses.setdefault(_text(node), []).append(node)
    reads: list[FollowedRead] = []
    followed_ids: set[int] = set()
    for helper in helpers:
        _bind_sinks(helper, language)
        if not helper.sinks or not helper.local or counts[helper.name] > 1:
            continue
        calls = calls_by_name.get(helper.name, [])
        other_uses = [
            use
            for use in identifier_uses.get(helper.name, [])
            if use.id != helper.name_node.id and use.id not in callee_ids
        ]
        if not calls or other_uses:
            continue
        helper_reads, helper_followed = _follow(helper, calls, language)
        reads.extend(helper_reads)
        followed_ids |= helper_followed
    # A line is followed only when every unresolved sink on it is.
    followed: set[tuple[WrapperSinkKind, int]] = set()
    unfollowed: set[tuple[WrapperSinkKind, int]] = set()
    for node in nodes:
        sink = _sink(node, language)
        if sink is None:
            continue
        key = (sink.kind, sink.line)
        if sink.node_id in followed_ids:
            followed.add(key)
        elif not _resolved(sink, language):
            unfollowed.add(key)
    unique = {(read.kind, read.name, read.default): read for read in reads}
    return WrapperAnalysis(
        parsed=True,
        reads=list(unique.values()),
        followed_sinks=[
            FollowedSink(kind=kind, line=line)
            for kind, line in sorted(followed - unfollowed, key=lambda key: (key[1], key[0].value))
        ],
    )

"""Read message payload types (classes and records) from C# and Java source.

For contract v2's message-schema check (``core.message_contract``): given a
source file, the classes, records and structs it declares, each with its
serialized members, using tree-sitter syntax trees (``tools.syntax_wrappers``):

- C#: a record's positional parameters; public instance properties and
  public instance fields (``static`` and ``const`` members skipped).
- Java: a record's components; instance fields of any visibility
  (``static`` and ``transient`` skipped), as Jackson-style serializers
  read them.

Nested types are read by their own (simple) names. Only names and declared
types leave this module. ``read_payload_types`` returns None when the file
can't be analysed (no grammar for its language, a parse error, tree-sitter
missing).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from src.core.message_contract import PayloadType
from src.tools.syntax_wrappers import SyntaxLanguage, parse, syntax_language

if TYPE_CHECKING:
    from tree_sitter import Node

_LANGUAGES: Final[frozenset[SyntaxLanguage]] = frozenset({SyntaxLanguage.CSHARP, SyntaxLanguage.JAVA})
_CSHARP_TYPES: Final[frozenset[str]] = frozenset({"class_declaration", "record_declaration", "struct_declaration", "record_struct_declaration"})
_JAVA_TYPES: Final[frozenset[str]] = frozenset({"class_declaration", "record_declaration"})


def is_payload_source(path: str) -> bool:
    """Whether ``path`` is in a language this module reads."""
    return syntax_language(path) in _LANGUAGES


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None and node.text else ""


def _compact(node: Node | None) -> str:
    return "".join(_text(node).split())


def _modifiers(node: Node) -> set[str]:
    words: set[str] = set()
    for child in node.children:
        if child.type in ("modifier", "modifiers"):
            words.update(_text(child).split())
    return words


def _csharp_members(declaration: Node) -> dict[str, str]:
    members: dict[str, str] = {}
    parameters = next((c for c in declaration.children if c.type == "parameter_list"), None)
    for parameter in parameters.children if parameters is not None else []:
        if parameter.type == "parameter":
            name = parameter.child_by_field_name("name")
            if name is not None:
                members[_text(name)] = _compact(parameter.child_by_field_name("type"))
    body = next((c for c in declaration.children if c.type == "declaration_list"), None)
    for member in body.children if body is not None else []:
        modifiers = _modifiers(member)
        if "public" not in modifiers or modifiers & {"static", "const"}:
            continue
        if member.type == "property_declaration":
            name = member.child_by_field_name("name")
            if name is not None:
                members[_text(name)] = _compact(member.child_by_field_name("type"))
        elif member.type == "field_declaration":
            variables = next((c for c in member.children if c.type == "variable_declaration"), None)
            if variables is None:
                continue
            written = _compact(variables.child_by_field_name("type"))
            for declarator in variables.children:
                if declarator.type == "variable_declarator":
                    name = declarator.child_by_field_name("name") or next(
                        (c for c in declarator.children if c.type == "identifier"), None
                    )
                    if name is not None:
                        members[_text(name)] = written
    return members


def _java_members(declaration: Node) -> dict[str, str]:
    members: dict[str, str] = {}
    parameters = declaration.child_by_field_name("parameters")
    for parameter in parameters.children if parameters is not None else []:
        if parameter.type == "formal_parameter":
            name = parameter.child_by_field_name("name")
            if name is not None:
                members[_text(name)] = _compact(parameter.child_by_field_name("type"))
    body = declaration.child_by_field_name("body")
    for member in body.children if body is not None else []:
        if member.type != "field_declaration" or _modifiers(member) & {"static", "transient"}:
            continue
        written = _compact(member.child_by_field_name("type"))
        for declarator in member.children:
            if declarator.type == "variable_declarator":
                name = declarator.child_by_field_name("name")
                if name is not None:
                    members[_text(name)] = written
    return members


def read_payload_types(content: str, path: str) -> dict[str, PayloadType] | None:
    """The types ``path`` declares, by simple name; None when unreadable.

    A name declared twice in the file (partial classes, nested types of the
    same name) keeps the members of every declaration merged.
    """
    language = syntax_language(path)
    if language not in _LANGUAGES:
        return None
    root = parse(content, path)
    if root is None:
        return None
    kinds = _CSHARP_TYPES if language is SyntaxLanguage.CSHARP else _JAVA_TYPES
    types: dict[str, PayloadType] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.children))
        if node.type not in kinds:
            continue
        name = _text(node.child_by_field_name("name"))
        if not name:
            continue
        members = _csharp_members(node) if language is SyntaxLanguage.CSHARP else _java_members(node)
        known = types.setdefault(name, PayloadType(name=name, language=language.value))
        known.properties.update(members)
    return types

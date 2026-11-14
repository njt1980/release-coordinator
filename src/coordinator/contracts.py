"""Contract compatibility evidence for one pull request (plan step 2e).

The deployment order rests on the declared change type (additive or
breaking). This module checks it against the contract files the pull
request changes: each changed ``.proto`` file is read at the base and the
head and compared (``core.proto_contract``). The result
(``ContractCheck``) decides whether the plan's compatibility is verified:

1. A changed contract file that can't be read or understood: UNREADABLE.
2. Any breaking difference: BREAKING (reason code CONTRACT_BREAKING).
3. Under the policy's ``contract_scope: strict`` (the default) the change
   is COMPATIBLE only when every changed file is a contract file;
   otherwise CODE_CHANGED (or NO_CONTRACT when no contract file changed).
4. Under ``contract_scope: interfaces`` a pull request that also changes
   code is COMPATIBLE when each changed service serves a contract and every
   caller of it compiles one of those contracts as a client (``.NET
   <Protobuf GrpcServices>`` links recorded by the scanner); otherwise
   NO_CONTRACT, naming the callers no contract covers.

Contract files are ``.proto`` files and OpenAPI documents (named
``openapi*``/``swagger*``/``*.openapi.*``, YAML or JSON; see
``core.openapi_contract``). A service whose directory holds an OpenAPI
document serves it (``ContractLink`` SERVER, recorded by the scanner) unless
its build references it as a client (``<OpenApiReference>``).

Message payload types (contract v2): a changed C# or Java source file
declaring the class or record a ``message_type`` channel is named after
(eShop ``IEventBus``, MassTransit) is compared too (``core.message_contract``);
a removed or retyped property is breaking for the channel's consumers. These
files are code, not contract files, so they never make a change COMPATIBLE
on their own. A file named after a message type that can't be read or
parsed is unreadable; other candidate files (under an ``*Event*``,
``*Message*`` or ``*Contract*`` directory) are compared when they parse.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable
from typing import Final

from src.core.models import (
    REPOSITORY_METADATA_KEY,
    SERVICE_ROOT_METADATA_KEY,
    ContractCheck,
    ContractCheckStatus,
    ContractLink,
    ContractScope,
    DependencyEdge,
    ChannelKind,
    KnowledgeGraph,
    MessageChannel,
    RepositoryCoverage,
)
from src.core.message_contract import PayloadType, diff_payload
from src.core.openapi_contract import OpenApiParseError, diff_openapi, is_openapi_file, parse_openapi
from src.core.operation_usage import (
    ObservedOperation,
    OperationAttribution,
    attribute_operations,
    broken_operations,
)
from src.core.proto_contract import ContractFinding, ProtoParseError, diff_protos, parse_proto

CONTRACT_SUFFIXES: Final[tuple[str, ...]] = (".proto",)
_PAYLOAD_SUFFIXES: Final[tuple[str, ...]] = (".cs", ".java")
_PAYLOAD_DIRECTORY: Final[re.Pattern[str]] = re.compile(r"(?:^|/)[^/]*(?:event|message|contract)[^/]*/", re.IGNORECASE)

# Reads a repository file at a commit; None when it doesn't exist there.
FileReader = Callable[[str, str], str | None]
# Reads the payload types a source file declares; None when it can't be analysed.
PayloadReader = Callable[[str, str], dict[str, PayloadType] | None]
# Reads the requests the named callee roles served (see core.operation_usage);
# None when telemetry can't answer.
UsageReader = Callable[[list[str]], list[ObservedOperation] | None]


def is_contract_file(path: str) -> bool:
    return path.lower().endswith(CONTRACT_SUFFIXES) or is_openapi_file(path)




def _short(service_id: str) -> str:
    return service_id.rsplit(":", 1)[-1]


def read_contract_diffs(
    read: FileReader, paths: Iterable[str], base: str, head: str
) -> tuple[list[ContractFinding], dict[str, str]]:
    """Compare each contract file at ``base`` and ``head``.

    Returns the findings and the files that couldn't be read or parsed
    (path to why). A file absent at both commits is unreadable: the pull
    request lists it, so something is wrong with the read.
    """
    findings: list[ContractFinding] = []
    unreadable: dict[str, str] = {}
    for path in paths:
        openapi = is_openapi_file(path)
        texts: list[str | None] = []
        try:
            for commit in (base, head):
                texts.append(read(path, commit))
        except Exception as error:  # a failed read, as the source client raised it
            unreadable[path] = f"couldn't be read: {error}"
            continue
        if texts[0] is None and texts[1] is None:
            unreadable[path] = "found at neither the base nor the head"
            continue
        try:
            if openapi:
                documents = [None if text is None else parse_openapi(text, path) for text in texts]
                findings.extend(diff_openapi(path, documents[0], documents[1]))
            else:
                protos = [None if text is None else parse_proto(text) for text in texts]
                findings.extend(diff_protos(path, protos[0], protos[1]))
        except (ProtoParseError, OpenApiParseError) as error:
            unreadable[path] = f"not understood: {error}"
    return findings, unreadable


def message_type_consumers(channels: Iterable[MessageChannel]) -> dict[str, list[str]]:
    """Each ``message_type`` channel's name (its payload type) and the
    services consuming it, across brokers."""
    consumers: dict[str, set[str]] = {}
    for channel in channels:
        if channel.kind is ChannelKind.MESSAGE_TYPE:
            consumers.setdefault(channel.name, set()).update(channel.consumers)
    return {name: sorted(services) for name, services in sorted(consumers.items())}


def payload_candidates(paths: Iterable[str], type_names: Iterable[str]) -> tuple[list[str], list[str]]:
    """Changed source files that may declare a message payload type:
    ``(named, nearby)``: files named after a type, and other files under an
    ``*Event*``/``*Message*``/``*Contract*`` directory."""
    names = set(type_names)
    named: list[str] = []
    nearby: list[str] = []
    if not names:
        return named, nearby
    for path in (p.lstrip("/") for p in paths):
        if not path.lower().endswith(_PAYLOAD_SUFFIXES):
            continue
        stem = posixpath.splitext(posixpath.basename(path))[0]
        if stem in names:
            named.append(path)
        elif _PAYLOAD_DIRECTORY.search(path):
            nearby.append(path)
    return named, nearby


def read_payload_diffs(
    read: FileReader,
    parse_payloads: PayloadReader,
    named: Iterable[str],
    nearby: Iterable[str],
    type_names: Iterable[str],
    base: str,
    head: str,
) -> tuple[list[ContractFinding], dict[str, str]]:
    """Compare the message payload types the candidate files declare at
    ``base`` and ``head`` (see the module doc for which are unreadable)."""
    names = set(type_names)
    findings: list[ContractFinding] = []
    unreadable: dict[str, str] = {}
    strict = set(named)
    for path in [*named, *nearby]:
        versions: list[dict[str, PayloadType]] = []
        try:
            for commit in (base, head):
                text = read(path, commit)
                if text is None:
                    versions.append({})
                    continue
                types = parse_payloads(text, path)
                if types is None:
                    raise ValueError("its syntax couldn't be analysed")
                versions.append(types)
        except Exception as error:
            if path in strict:
                unreadable[path] = f"message type couldn't be compared: {error}"
            continue
        for name in sorted(names & (versions[0].keys() | versions[1].keys())):
            findings.extend(diff_payload(path, name, versions[0].get(name), versions[1].get(name)))
    return findings, unreadable


def contract_links(
    graph: KnowledgeGraph, head_coverage: Iterable[RepositoryCoverage] = ()
) -> dict[str, list[tuple[str, ContractLink]]]:
    """Each registered service's contract links, as ``(repository, link)``.

    The head's coverage (the pull request's changed services, scanned at
    the head) replaces the baseline's for the same repository and root.
    """
    by_root: dict[tuple[str, str], list[ContractLink]] = {}
    for coverage in [*(graph.static_coverage or []), *head_coverage]:
        for service in coverage.services:
            by_root[(coverage.repository.lower(), service.root)] = list(service.contracts)
    links: dict[str, list[tuple[str, ContractLink]]] = {}
    for node in graph.services:
        repository = str(node.metadata.get(REPOSITORY_METADATA_KEY, "")).lower()
        root = node.metadata.get(SERVICE_ROOT_METADATA_KEY)
        if repository and root is not None and (repository, root) in by_root:
            links[node.service_id] = [(repository, link) for link in by_root[(repository, root)]]
    return links


def contract_consumers(
    links: dict[str, list[tuple[str, ContractLink]]], repository: str, paths: Iterable[str]
) -> dict[str, list[str]]:
    """For each contract file, the services compiling it as a client."""
    wanted = {path.lstrip("/") for path in paths}
    consumers: dict[str, set[str]] = {path: set() for path in wanted}
    for service, service_links in links.items():
        for link_repository, link in service_links:
            if link_repository == repository.lower() and link.path in wanted and link.role.calls:
                consumers[link.path].add(service)
    return {path: sorted(services) for path, services in sorted(consumers.items())}


def contract_callers(
    links: dict[str, list[tuple[str, ContractLink]]],
    edges: Iterable[DependencyEdge],
    repository: str,
    paths: Iterable[str],
) -> dict[str, list[str]]:
    """For each contract file, the services calling a service that serves
    it (dependency edges into the server), whatever contract they compile."""
    wanted = {path.lstrip("/") for path in paths}
    servers: dict[str, set[str]] = {}
    for service, service_links in links.items():
        for link_repository, link in service_links:
            if link_repository == repository.lower() and link.path in wanted and link.role.serves:
                servers.setdefault(link.path, set()).add(service)
    callers: dict[str, set[str]] = {}
    edge_list = list(edges)
    for path, served_by in servers.items():
        for edge in edge_list:
            if edge.target_service_id in served_by and edge.source_service_id not in served_by:
                callers.setdefault(path, set()).add(edge.source_service_id)
    return {path: sorted(services) for path, services in sorted(callers.items())}


def uncovered_callers(
    links: dict[str, list[tuple[str, ContractLink]]],
    targets: Iterable[str],
    edges: Iterable[DependencyEdge],
) -> list[str]:
    """Callers of ``targets`` that reach them through no shared contract.

    A changed service that serves no contract at all is listed too (as
    itself), since nothing describes its interface.
    """
    served = {
        target: {(repo, link.path) for repo, link in links.get(target, []) if link.role.serves}
        for target in targets
    }
    uncovered: set[str] = {target for target, contracts in served.items() if not contracts}
    for edge in edges:
        caller, callee = edge.source_service_id, edge.target_service_id
        if callee not in served or caller == callee:
            continue
        calls = {(repo, link.path) for repo, link in links.get(caller, []) if link.role.calls}
        if not calls & served[callee]:
            uncovered.add(caller)
    return sorted(uncovered)


def _describe(check: ContractCheck) -> str:
    files = ", ".join(check.contract_files)
    if check.status is ContractCheckStatus.BREAKING:
        files = ", ".join(dict.fromkeys(finding.path for finding in check.breaking_findings))
    if check.status is ContractCheckStatus.UNREADABLE:
        return "Contract files couldn't be checked: " + "; ".join(
            f"{path} {why}" for path, why in check.unreadable.items()
        )
    if check.status is ContractCheckStatus.BREAKING:
        broken = check.breaking_findings
        return f"{len(broken)} breaking contract change(s) in {files}: " + " ".join(
            finding.detail for finding in broken[:5]
        ) + (" …" if len(broken) > 5 else "")
    if check.status is ContractCheckStatus.CODE_CHANGED:
        return (
            f"The contract changes in {files} are compatible, but the pull request also changes "
            f"{len(check.other_files)} other file(s), and the project's contract scope is strict."
        )
    if check.status is ContractCheckStatus.NO_CONTRACT:
        if check.uncovered_callers:
            names = ", ".join(_short(s) for s in check.uncovered_callers[:8])
            return f"No shared contract covers these callers or changed services: {names}."
        return "No contract file changed, so no contract evidence covers this change."
    changes = len(check.findings)
    if check.other_files:
        return (
            f"Every caller reaches the changed services through a contract, and the "
            f"{changes} contract change(s) are compatible."
        )
    return f"Only contract files changed ({files}); all {changes} change(s) are compatible."


def evaluate_contracts(
    scope: ContractScope,
    changed_files: Iterable[str],
    findings: list[ContractFinding],
    unreadable: dict[str, str],
    consumers: dict[str, list[str]],
    uncovered: list[str],
    callers: dict[str, list[str]] | None = None,
    message_consumers: dict[str, list[str]] | None = None,
) -> ContractCheck:
    """Decide the contract check's status (see the module doc for the order).

    ``findings`` and ``unreadable`` include message payload types';
    ``message_consumers`` maps each compared message type to its consumers.
    """
    files = [path.lstrip("/") for path in changed_files]
    contract_files = [path for path in files if is_contract_file(path)]
    other_files = [path for path in files if not is_contract_file(path)]
    breaking = any(finding.breaking for finding in findings)
    if unreadable:
        status = ContractCheckStatus.UNREADABLE
    elif breaking:
        status = ContractCheckStatus.BREAKING
    elif not other_files and contract_files:
        status = ContractCheckStatus.COMPATIBLE
    elif scope is ContractScope.STRICT:
        status = ContractCheckStatus.CODE_CHANGED if contract_files else ContractCheckStatus.NO_CONTRACT
    elif uncovered:
        status = ContractCheckStatus.NO_CONTRACT
    else:
        status = ContractCheckStatus.COMPATIBLE
    check = ContractCheck(
        scope=scope,
        status=status,
        contract_files=contract_files,
        other_files=other_files,
        findings=findings,
        unreadable=unreadable,
        consumers=consumers,
        callers=dict(callers or {}),
        message_consumers=dict(message_consumers or {}),
        uncovered_callers=uncovered if scope is ContractScope.INTERFACES and other_files else [],
        detail="-",
    )
    return check.model_copy(update={"detail": _describe(check)})


def _role_services(graph: KnowledgeGraph) -> dict[str, str]:
    """Lower-case service name -> service id, for names only one service has."""
    by_name: dict[str, set[str]] = {}
    for node in graph.services:
        by_name.setdefault(node.name.strip().lower(), set()).add(node.service_id)
    return {name: next(iter(ids)) for name, ids in by_name.items() if len(ids) == 1}


def attribute_contract_operations(
    check: ContractCheck,
    links: dict[str, list[tuple[str, ContractLink]]],
    graph: KnowledgeGraph,
    repository: str,
    read_usage: UsageReader,
    lookback_days: int,
) -> OperationAttribution | None:
    """Which callers were seen using each broken OpenAPI operation (advisory).

    Only a BREAKING check with breaking OpenAPI findings that name an
    operation is attributed, per document: the services serving it are the
    callees, its consumers and callers the candidates. None when nothing is
    attributable or telemetry can't answer. Operations are labelled
    ``METHOD /path``, prefixed with the document when several broke.
    """
    if check.status is not ContractCheckStatus.BREAKING:
        return None
    documents = [path for path in check.contract_files if is_openapi_file(path)]
    broken = broken_operations(check.breaking_findings, documents)
    if not broken:
        return None
    names = {node.service_id: node.name for node in graph.services}
    servers: dict[str, list[str]] = {}
    for service, service_links in links.items():
        for link_repository, link in service_links:
            if link_repository == repository.lower() and link.path in broken and link.role.serves:
                servers.setdefault(link.path, []).append(service)
    callee_names = sorted({names[s] for served in servers.values() for s in served if s in names})
    if not callee_names:
        return None
    observed = read_usage(callee_names)
    if observed is None:
        return None
    role_services = _role_services(graph)
    merged = OperationAttribution(lookback_days=lookback_days)
    seen: set[str] = set()
    unattributed: set[str] = set()
    for document, operations in broken.items():
        callees = {names[s].strip().lower() for s in servers.get(document, []) if s in names}
        candidates = {*check.callers.get(document, []), *check.consumers.get(document, [])}
        if not callees or not candidates:
            continue
        rows = [row for row in observed if row.callee.strip().lower().rsplit("/", 1)[-1] in callees]
        result = attribute_operations(operations, candidates, rows, role_services, lookback_days)
        prefix = f"{document}: " if len(broken) > 1 else ""
        for operation, services in result.operation_callers.items():
            merged.operation_callers[prefix + operation] = services
        seen.update(service for services in result.operation_callers.values() for service in services)
        seen.update(result.other_operations_only)
        unattributed.update(result.unattributed)
        merged.unmatched_roles = sorted({*merged.unmatched_roles, *result.unmatched_roles})
    if not merged.operation_callers:
        return None
    attributed = {s for services in merged.operation_callers.values() for s in services}
    merged.other_operations_only = sorted(seen - attributed)
    merged.unattributed = sorted(unattributed - seen)
    return merged

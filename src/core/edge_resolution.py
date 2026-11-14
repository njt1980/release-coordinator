"""Resolve dependency endpoints against a service inventory.

Shared by discovery (reconciling a baseline) and the coordinator (resolving
a pull request head's references against the baseline it is assessed
against), so both resolve a reference the same way.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final

from src.core.config_resolution import ResolutionOutcome, resolve_variable
from src.core.env_names import endpoint_of_value, is_endpoint_variable
from src.core.models import (
    REPOSITORY_METADATA_KEY,
    DependencyEdge,
    DiscoveryMethod,
    SERVICE_ROOT_METADATA_KEY,
    PipelineVariables,
    ServiceEnvironment,
    ServiceNode,
    ServiceIdentity,
    ServiceStaticConfig,
    StaticDefaultUse,
    UnresolvedEntity,
    UnresolvedEntityKind,
)
from src.core.service_ids import (
    IdentityContext,
    IdentityIndex,
    is_unset_env_var_reference,
    parse_env_var_reference,
    resolve_env_var_reference,
    resolve_reference,
    unresolved_entity,
)
from src.core.telemetry_capability import wire_protocol

# Provenance of an edge a workload's Kubernetes environment declares
# (``environment_edges``), as opposed to a read found in code.
K8S_ENV_PROVENANCE: Final[str] = "kubernetes_env"


def service_environment_index(
    environments: list[ServiceEnvironment],
) -> dict[str, ServiceEnvironment]:
    """Map service FQURIs to everything Kubernetes sets for them.

    Entries for the same service are merged; a name opaque in any entry is
    not resolved in the merged one.
    """
    index: dict[str, ServiceEnvironment] = {}
    for environment in environments:
        existing = index.get(environment.service_id)
        if existing is None:
            index[environment.service_id] = environment
            continue
        opaque = {*existing.opaque_variables, *environment.opaque_variables}
        variables = {**existing.variables, **environment.variables}
        conflicting = {
            name
            for name in existing.variables.keys() & environment.variables.keys()
            if existing.variables[name] != environment.variables[name]
        }
        opaque |= conflicting
        index[environment.service_id] = ServiceEnvironment(
            service_id=environment.service_id,
            variables={k: v for k, v in variables.items() if k not in opaque},
            variable_sources={
                name: sorted(
                    {
                        *existing.variable_sources.get(name, []),
                        *environment.variable_sources.get(name, []),
                    }
                )
                for name in variables
                if name not in opaque
                and (name in existing.variable_sources or name in environment.variable_sources)
            },
            opaque_variables=sorted(opaque),
            withheld_variables=sorted(
                {*existing.withheld_variables, *environment.withheld_variables}
            ),
            has_opaque_sources=existing.has_opaque_sources or environment.has_opaque_sources,
        )
    return index


def pipeline_index(
    services: Iterable[ServiceNode], pipelines: Iterable[PipelineVariables] | None
) -> dict[str, PipelineVariables] | None:
    """Map each repository-scanned service's FQURI to its repository's pipelines.

    None when ``pipelines`` is None (a snapshot predating pipeline records),
    so resolution behaves as it did when that snapshot was written.
    """
    if pipelines is None:
        return None
    by_repository = {pipeline.repository.lower(): pipeline for pipeline in pipelines}
    index: dict[str, PipelineVariables] = {}
    for service in services:
        repository = str(service.metadata.get(REPOSITORY_METADATA_KEY, "")).lower()
        if repository in by_repository:
            index[service.service_id] = by_repository[repository]
    return index


def static_config_index(
    services: Iterable[ServiceNode], configs: Iterable[ServiceStaticConfig] | None
) -> dict[str, ServiceStaticConfig] | None:
    """Map each repository-scanned service's FQURI to its static configuration.

    Matched by repository and service root. None when ``configs`` is None
    (a snapshot predating static configuration records), so resolution
    behaves as it did when that snapshot was written.
    """
    if configs is None:
        return None
    by_root = {(config.repository.lower(), config.root): config for config in configs}
    index: dict[str, ServiceStaticConfig] = {}
    for service in services:
        repository = str(service.metadata.get(REPOSITORY_METADATA_KEY, "")).lower()
        root = str(service.metadata.get(SERVICE_ROOT_METADATA_KEY, ""))
        config = by_root.get((repository, root))
        if repository and config is not None:
            index[service.service_id] = config
    return index


def _record(unresolved: dict[str, UnresolvedEntity], entity: UnresolvedEntity) -> str:
    unresolved.setdefault(entity.entity_id, entity)
    return entity.entity_id


def _resolve_configured(
    reference: str,
    index: IdentityIndex,
    context: IdentityContext,
    environments: dict[str, ServiceEnvironment],
    unresolved: dict[str, UnresolvedEntity],
    pipelines: Mapping[str, PipelineVariables] | None,
    configs: Mapping[str, ServiceStaticConfig],
    defaults_used: dict[tuple[str, str], StaticDefaultUse] | None,
) -> str | None:
    """Resolve an ``env:`` reference through every configuration layer.

    None when the reference isn't an ``env:`` reference of a registered
    service (the caller falls back to plain resolution).
    """
    parsed = parse_env_var_reference(reference)
    if parsed is None:
        return None
    service_name, variable = parsed
    owner = resolve_reference(service_name, index, context)
    if owner is None:
        return None
    resolution = resolve_variable(
        variable,
        configs.get(owner.uri),
        environments.get(owner.uri),
        pipelines.get(owner.uri) if pipelines is not None else None,
    )
    owner_context = context.in_namespace(owner.namespace)
    entity = unresolved_entity(reference)
    if resolution.outcome is ResolutionOutcome.UNSET:
        entity = entity.model_copy(update={"kind": UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET})
    if resolution.endpoint is None:
        return _record(unresolved, entity)
    identity = resolve_reference(resolution.endpoint, index, owner_context)
    if resolution.outcome is ResolutionOutcome.RUNTIME:
        return identity.uri if identity else _record(unresolved, entity)
    target = identity.uri if identity else _record(unresolved, unresolved_entity(resolution.endpoint))
    if defaults_used is not None and resolution.origin is not None:
        defaults_used[(owner.uri, variable)] = StaticDefaultUse(
            service_id=owner.uri,
            variable=variable,
            endpoint=resolution.endpoint,
            origin=resolution.origin,
            file_path=resolution.file_path or "Dockerfile",
            profile=resolution.profile,
            target=target,
        )
    return target


def resolve_endpoint(
    reference: str,
    index: IdentityIndex,
    context: IdentityContext,
    environments: dict[str, ServiceEnvironment],
    unresolved: dict[str, UnresolvedEntity],
    pipelines: Mapping[str, PipelineVariables] | None = None,
    configs: Mapping[str, ServiceStaticConfig] | None = None,
    defaults_used: dict[tuple[str, str], StaticDefaultUse] | None = None,
) -> str:
    """Return the node ID an edge endpoint points at.

    References that resolve to exactly one registered service become that
    service's FQURI, including ``env:`` references whose Kubernetes value
    names one. Anything else is recorded in ``unresolved`` and its
    unresolved-entity ID is returned; an ``env:`` reference to a variable
    the service's workloads provably don't set, and its repository's
    pipelines don't define (see ``pipeline_index``), is
    ENVIRONMENT_VARIABLE_UNSET. Pipeline values never resolve a reference:
    the cluster's environment already reflects deploy-time substitution.

    With ``configs`` (see ``static_config_index``), an ``env:`` reference
    is resolved through ``config_resolution``: a connection string's server
    counts, and when nothing at runtime sets the variable the image's
    ``ENV`` or a configuration default applies; each such use is added to
    ``defaults_used``.
    """
    if configs is not None:
        target = _resolve_configured(
            reference, index, context, environments, unresolved, pipelines, configs, defaults_used
        )
        if target is not None:
            return target
    values = {uri: env.variables for uri, env in environments.items()}
    identity = resolve_reference(
        reference, index, context
    ) or resolve_env_var_reference(reference, values, index, context)
    if identity is not None:
        return identity.uri
    entity = unresolved_entity(reference)
    if is_unset_env_var_reference(reference, environments, index, context, pipelines):
        entity = entity.model_copy(
            update={"kind": UnresolvedEntityKind.ENVIRONMENT_VARIABLE_UNSET}
        )
    unresolved.setdefault(entity.entity_id, entity)
    return entity.entity_id


def target_context(source_id: str, context: IdentityContext) -> IdentityContext:
    """Return the context a target's bare name resolves in.

    A registered source's own namespace, as in-cluster DNS resolves short
    names in the caller's namespace; ``context`` otherwise.
    """
    try:
        source = ServiceIdentity.parse(source_id)
    except ValueError:
        return context
    return context.in_namespace(source.namespace)


def reference_protocol(
    reference: str,
    index: IdentityIndex,
    context: IdentityContext,
    environments: dict[str, ServiceEnvironment],
    pipelines: Mapping[str, PipelineVariables] | None,
    configs: Mapping[str, ServiceStaticConfig] | None,
) -> str | None:
    """The wire protocol an ``env:`` reference's resolved address names.

    None for anything else, or when the address has no scheme (a bare host,
    a connection string's server).
    """
    parsed = parse_env_var_reference(reference)
    if parsed is None:
        return None
    service_name, variable = parsed
    owner = resolve_reference(service_name, index, context)
    if owner is None:
        return None
    resolution = resolve_variable(
        variable,
        configs.get(owner.uri) if configs is not None else None,
        environments.get(owner.uri),
        pipelines.get(owner.uri) if pipelines is not None else None,
    )
    return wire_protocol(resolution.endpoint) if resolution.endpoint else None


def _imported_config_map(source: str) -> str | None:
    """The config map a ``variable_sources`` entry imports whole (envFrom), or None."""
    _, separator, origin = source.rpartition(":configMap/")
    return origin if separator else None


def shared_config_maps(environments: Mapping[str, ServiceEnvironment]) -> set[tuple[str, str]]:
    """``(namespace, config map)`` pairs more than one service imports whole.

    Such a config map is an address book: every workload importing it gets
    every address, whatever it calls.
    """
    importers: dict[tuple[str, str], set[str]] = {}
    for service_id, environment in environments.items():
        try:
            namespace = ServiceIdentity.parse(service_id).namespace
        except ValueError:
            continue
        for sources in environment.variable_sources.values():
            for source in sources:
                name = _imported_config_map(source)
                if name is not None:
                    importers.setdefault((namespace, name), set()).add(service_id)
    return {key for key, services in importers.items() if len(services) > 1}


def from_shared_import(sources: Sequence[str], shared: Collection[str]) -> bool:
    """Whether a variable's value only comes from importing whole a config
    map named in ``shared`` (see ``shared_config_maps``).

    An explicit ``env`` entry (a literal or ``configMapKeyRef``), or a
    config map only this service imports, is a decision to wire the
    variable into this workload. No recorded source (older snapshots)
    counts as explicit.
    """
    names = [_imported_config_map(source) for source in sources]
    return bool(names) and all(name is not None and name in shared for name in names)


def environment_addresses(
    variables: Mapping[str, str],
    sources: Mapping[str, Sequence[str]] | None = None,
    shared: Collection[str] = (),
) -> list[tuple[str, str]]:
    """(variable, address) for each endpoint-like variable a workload's
    Kubernetes environment sets to an address (a URL, ``host:port``, a bare
    host or a connection string's server), in name order.

    Only names that denote an endpoint count (``*_URL``, ``*_HOST``,
    ``*_ADDR``, ``ConnectionStrings__*``...; see ``is_endpoint_variable``),
    so a value like ``APP_NAME=frontend`` is never read as a dependency.
    Variables only imported from a config map several services share
    (``from_shared_import``; ``shared`` names those config maps) don't
    count: importing an address book is no call to everything in it (code
    that reads one still makes that edge).
    """
    found: list[tuple[str, str]] = []
    for name in sorted(variables):
        if not is_endpoint_variable(name) or from_shared_import((sources or {}).get(name, ()), shared):
            continue
        address = endpoint_of_value(variables[name], connection_string=True)
        if address:
            found.append((name, address))
    return found


def environment_edges(
    environments: Mapping[str, ServiceEnvironment],
    index: IdentityIndex,
    context: IdentityContext,
    observed_at: datetime,
) -> list[DependencyEdge]:
    """Edges a workload's Kubernetes environment declares (gap: a dependency
    configured for a service whose code reads it in a way the scanner can't
    follow).

    Each endpoint-like variable whose value resolves, in the service's
    namespace, to exactly one registered service other than itself becomes
    an edge with ``K8S_ENV_PROVENANCE``: weaker evidence than a read in code
    (the variable may be unused), shown as such. Values that resolve to
    nothing add nothing (no new gaps).
    """
    edges: list[DependencyEdge] = []
    shared = shared_config_maps(environments)
    for service_id, environment in sorted(environments.items()):
        try:
            owner = ServiceIdentity.parse(service_id)
        except ValueError:
            continue
        owner_context = context.in_namespace(owner.namespace)
        in_namespace = {name for namespace, name in shared if namespace == owner.namespace}
        for _, address in environment_addresses(
            environment.variables, environment.variable_sources, in_namespace
        ):
            target = resolve_reference(address, index, owner_context)
            if target is None or target.uri == service_id:
                continue
            edges.append(
                DependencyEdge(
                    source_service_id=service_id,
                    target_service_id=target.uri,
                    discovery_methods=[DiscoveryMethod.K8S_MANIFEST],
                    confidence_score=0.5,
                    last_observed_at=observed_at,
                    provenance=K8S_ENV_PROVENANCE,
                    protocol=wire_protocol(address),
                )
            )
    return edges


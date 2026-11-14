"""Resolve dependency endpoints against a service inventory.

Shared by discovery (reconciling a baseline) and the coordinator (resolving
a pull request head's references against the baseline it is assessed
against), so both resolve a reference the same way.
"""

from collections.abc import Iterable, Mapping

from src.core.config_resolution import ResolutionOutcome, resolve_variable
from src.core.models import (
    REPOSITORY_METADATA_KEY,
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
from src.core.telemetry_capability import wire_protocol
from src.core.service_ids import (
    IdentityContext,
    IdentityIndex,
    is_unset_env_var_reference,
    parse_env_var_reference,
    resolve_env_var_reference,
    resolve_reference,
    unresolved_entity,
)


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


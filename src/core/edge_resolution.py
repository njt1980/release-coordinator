"""Resolve dependency endpoints against a service inventory.

Shared by discovery (reconciling a baseline) and the coordinator (resolving
a pull request head's references against the baseline it is assessed
against), so both resolve a reference the same way.
"""

from src.core.models import (
    ServiceEnvironment,
    ServiceIdentity,
    UnresolvedEntity,
    UnresolvedEntityKind,
)
from src.core.service_ids import (
    IdentityContext,
    IdentityIndex,
    is_unset_env_var_reference,
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


def resolve_endpoint(
    reference: str,
    index: IdentityIndex,
    context: IdentityContext,
    environments: dict[str, ServiceEnvironment],
    unresolved: dict[str, UnresolvedEntity],
) -> str:
    """Return the node ID an edge endpoint points at.

    References that resolve to exactly one registered service become that
    service's FQURI, including ``env:`` references whose Kubernetes value
    names one. Anything else is recorded in ``unresolved`` and its
    unresolved-entity ID is returned; an ``env:`` reference to a variable
    the service's workloads provably don't set is ENVIRONMENT_VARIABLE_UNSET.
    """
    values = {uri: env.variables for uri, env in environments.items()}
    identity = resolve_reference(
        reference, index, context
    ) or resolve_env_var_reference(reference, values, index, context)
    if identity is not None:
        return identity.uri
    entity = unresolved_entity(reference)
    if is_unset_env_var_reference(reference, environments, index, context):
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

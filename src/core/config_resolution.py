"""Decide what value a service runs with for one configuration variable.

Pure functions over the evidence a baseline records: the Kubernetes
container environment (``ServiceEnvironment``), the build pipelines
(``PipelineVariables``) and what source control declares
(``ServiceStaticConfig``: Dockerfile ``ENV`` and configuration defaults).

Precedence, highest first:

1. Kubernetes sets the variable: that value (RUNTIME).
2. Anything that may set it with a value we can't read (an opaque
   Kubernetes source, a pipeline, an image ``ENV`` built from an argument):
   OPAQUE, never guessed.
3. The image's Dockerfile ``ENV``: that value (STATIC_DEFAULT).
4. The configuration file for the active profile, then the base file, then
   the default in code (STATIC_DEFAULT).
5. Nothing: UNSET.

A default is only used when every layer above it provably doesn't set the
variable, so a service whose Kubernetes environment wasn't read never
resolves from a default.
"""

from collections.abc import Callable, Iterable
from enum import Enum
from typing import Final

from pydantic import BaseModel

from src.core.env_names import (
    endpoint_in_value,
    is_loopback_endpoint,
    names_host,
)
from src.core.models import (
    ConfigBinding,
    ConfigFramework,
    PipelineVariables,
    ServiceEnvironment,
    ServiceStaticConfig,
    StaticDefault,
    StaticDefaultOrigin,
    StaticDefaultSource,
)

_DOTNET_PROFILE_VARIABLES: Final[tuple[str, ...]] = (
    "ASPNETCORE_ENVIRONMENT",
    "DOTNET_ENVIRONMENT",
)
_DOTNET_DEFAULT_PROFILE: Final[str] = "Production"
_SPRING_PROFILE_VARIABLE: Final[str] = "SPRING_PROFILES_ACTIVE"


class LayerState(str, Enum):
    """What the runtime layers say about one variable."""

    SET = "SET"
    OPAQUE = "OPAQUE"
    UNSET = "UNSET"


class LayerValue(BaseModel):
    """One variable as the runtime layers (Kubernetes, pipelines, image) see it.

    ``from_image`` is true when the value comes from the image's ``ENV``
    rather than Kubernetes. ``detail`` says which layer made it opaque.
    """

    state: LayerState
    value: str | None = None
    from_image: bool = False
    detail: str = ""


class ResolutionOutcome(str, Enum):
    """Where the value a service runs with comes from.

    RUNTIME: Kubernetes sets it (possibly through a Spring placeholder).
    STATIC_DEFAULT: nothing at runtime sets it; a static default applies.
    OPAQUE: something may set it with a value we can't read.
    UNSET: nothing sets it and no default gives it an address.
    """

    RUNTIME = "RUNTIME"
    STATIC_DEFAULT = "STATIC_DEFAULT"
    OPAQUE = "OPAQUE"
    UNSET = "UNSET"


class ConfigResolution(BaseModel):
    """The address a service runs with for one variable, and where it came from.

    ``endpoint`` is set for RUNTIME and STATIC_DEFAULT (credential-free, see
    ``endpoint_in_value``); for RUNTIME a value that names no address is
    passed through as written. ``origin``, ``file_path`` and ``profile``
    describe a STATIC_DEFAULT. ``detail`` explains OPAQUE and UNSET.
    """

    outcome: ResolutionOutcome
    endpoint: str | None = None
    origin: StaticDefaultOrigin | None = None
    file_path: str | None = None
    profile: str | None = None
    detail: str = ""


def spring_variable(key: str) -> str:
    """Return the environment variable Spring's relaxed binding reads for ``key``.

    ``spring.datasource.url`` -> ``SPRING_DATASOURCE_URL``; dashes are
    dropped (``my-app.orders-url`` -> ``MYAPP_ORDERSURL``) and list indexes
    become segments (``hosts[0]`` -> ``HOSTS_0_``).
    """
    name = key.replace("[", "_").replace("]", "_").replace(".", "_").replace("-", "")
    return name.upper()


def dotnet_variable(key: str) -> str:
    """Return the environment variable that overrides a .NET key (``A:B`` -> ``A__B``)."""
    return key.replace(":", "__")


def spring_candidates(name: str) -> list[str]:
    """Names a Spring property or placeholder may be read from, in order.

    The name itself (a placeholder may name a variable directly), then the
    forms Spring's system-environment source checks.
    """
    forms = [
        name,
        name.replace(".", "_"),
        name.replace("-", "_"),
        name.replace(".", "_").replace("-", "_"),
        spring_variable(name),
    ]
    forms.extend(form.upper() for form in list(forms))
    return list(dict.fromkeys(forms))


def _matches(names: Iterable[str], candidates: list[str], case_insensitive: bool) -> list[str]:
    if case_insensitive:
        wanted = {candidate.lower() for candidate in candidates}
        return [name for name in names if name.lower() in wanted]
    wanted = set(candidates)
    return [name for name in names if name in wanted]


def lookup_variable(
    candidates: list[str],
    environment: ServiceEnvironment | None,
    pipeline: PipelineVariables | None,
    config: ServiceStaticConfig | None,
    case_insensitive: bool = False,
) -> LayerValue:
    """Return what the runtime layers say about a variable.

    ``candidates`` are the names it may be set under (first match wins).
    Without a read Kubernetes environment nothing is provably unset, so the
    variable is OPAQUE.
    """
    if environment is None:
        return LayerValue(state=LayerState.OPAQUE, detail="the Kubernetes environment wasn't read")
    found = _matches(environment.variables, candidates, case_insensitive)
    if found:
        return LayerValue(state=LayerState.SET, value=environment.variables[found[0]])
    hidden = [*environment.opaque_variables, *environment.withheld_variables]
    if _matches(hidden, candidates, case_insensitive):
        return LayerValue(state=LayerState.OPAQUE, detail="Kubernetes sets it with a value we can't read")
    if environment.has_opaque_sources:
        return LayerValue(
            state=LayerState.OPAQUE,
            detail="the workload imports a Secret or config map that may set it",
        )
    if pipeline is not None and _pipeline_may_define(pipeline, candidates, case_insensitive):
        return LayerValue(state=LayerState.OPAQUE, detail="a build pipeline may set it")
    if config is not None:
        found = _matches(config.image_variables, candidates, case_insensitive)
        if found:
            return LayerValue(
                state=LayerState.SET, value=config.image_variables[found[0]], from_image=True
            )
        hidden = [*config.image_opaque_variables, *config.image_withheld_variables]
        if _matches(hidden, candidates, case_insensitive):
            return LayerValue(
                state=LayerState.OPAQUE, detail="the image sets it from a build argument"
            )
    return LayerValue(state=LayerState.UNSET)


def _pipeline_may_define(
    pipeline: PipelineVariables, candidates: list[str], case_insensitive: bool
) -> bool:
    if not pipeline.readable or pipeline.opaque_sources:
        return True
    names = [*pipeline.variables, *pipeline.withheld_variables, *pipeline.opaque_variables]
    return bool(_matches(names, candidates, case_insensitive))


Lookup = Callable[[str], LayerValue]


class _Evaluated(BaseModel):
    value: str | None
    state: LayerState
    from_runtime: bool = False
    detail: str = ""


def _split_placeholder(body: str) -> tuple[str, str | None]:
    """Split ``name:default`` at the first colon outside a nested placeholder."""
    depth = 0
    for position, char in enumerate(body):
        if body.startswith("${", position):
            depth += 1
        elif char == "}" and depth:
            depth -= 1
        elif char == ":" and depth == 0:
            return body[:position], body[position + 1 :]
    return body, None


def placeholders(text: str) -> list[tuple[str, str | None, int, int]]:
    """Return the top-level ``${name[:default]}`` placeholders in ``text``.

    Each is ``(name, default, start, end)``; ``end`` is exclusive. An
    unterminated placeholder ends the scan.
    """
    found: list[tuple[str, str | None, int, int]] = []
    position = 0
    while True:
        start = text.find("${", position)
        if start < 0:
            return found
        depth, cursor = 0, start
        while cursor < len(text):
            if text.startswith("${", cursor):
                depth += 1
                cursor += 2
                continue
            if text[cursor] == "}":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if depth:
            return found
        name, default = _split_placeholder(text[start + 2 : cursor])
        found.append((name.strip(), default, start, cursor + 1))
        position = cursor + 1


def evaluate_template(template: str, lookup: Lookup) -> _Evaluated:
    """Fill a Spring template's placeholders from the runtime layers.

    A placeholder the runtime sets takes that value; an unset one its
    default (itself evaluated); without a default the template is UNSET.
    Any opaque placeholder makes the template OPAQUE.
    """
    parts: list[str] = []
    position = 0
    from_runtime = False
    for name, default, start, end in placeholders(template):
        parts.append(template[position:start])
        position = end
        layer = lookup(name)
        if layer.state is LayerState.OPAQUE:
            return _Evaluated(value=None, state=LayerState.OPAQUE, detail=f"{name}: {layer.detail}")
        if layer.state is LayerState.SET and layer.value is not None:
            parts.append(layer.value)
            from_runtime = from_runtime or not layer.from_image
            continue
        if default is None:
            return _Evaluated(
                value=None, state=LayerState.UNSET, detail=f"placeholder {name} has no default"
            )
        inner = evaluate_template(default, lookup)
        if inner.state is not LayerState.SET or inner.value is None:
            return inner
        parts.append(inner.value)
        from_runtime = from_runtime or inner.from_runtime
    parts.append(template[position:])
    return _Evaluated(value="".join(parts), state=LayerState.SET, from_runtime=from_runtime)


def active_profiles(framework: ConfigFramework, lookup: Lookup) -> list[str] | None:
    """Return the active configuration profiles, last one winning; None if unknown."""
    if framework is ConfigFramework.DOTNET:
        for variable in _DOTNET_PROFILE_VARIABLES:
            layer = lookup(variable)
            if layer.state is LayerState.OPAQUE:
                return None
            if layer.state is LayerState.SET and layer.value:
                return [layer.value.strip()]
        return [_DOTNET_DEFAULT_PROFILE]
    if framework is ConfigFramework.SPRING:
        layer = lookup(_SPRING_PROFILE_VARIABLE)
        if layer.state is LayerState.OPAQUE:
            return None
        if layer.state is LayerState.SET and layer.value:
            return [p.strip() for p in layer.value.split(",") if p.strip()]
        return []
    return []


class _Choice(BaseModel):
    default: StaticDefault | None = None
    ambiguous: bool = False


def _single(defaults: list[StaticDefault]) -> _Choice | None:
    """One tier's choice: None when the tier is empty."""
    if not defaults:
        return None
    if len({d.template for d in defaults}) > 1:
        return _Choice(ambiguous=True)
    return _Choice(default=defaults[0])


def choose_default(defaults: list[StaticDefault], profiles: list[str] | None) -> _Choice:
    """Pick the default a service falls back to under ``profiles``.

    Active profile files (the last active profile first), then base files,
    then code defaults. Unknown profiles: the active one may have its own
    file or none, so every file (profile or base) is a candidate and they
    must agree. Disagreeing defaults in one tier are ambiguous.
    """
    files = [d for d in defaults if d.source is StaticDefaultSource.CONFIG_FILE]
    tiers: list[list[StaticDefault]] = []
    if profiles is None:
        tiers.append(files)
    else:
        for profile in reversed(profiles):
            tiers.append([d for d in files if (d.profile or "").lower() == profile.lower()])
        tiers.append([d for d in files if d.profile is None])
    tiers.append([d for d in defaults if d.source is StaticDefaultSource.CODE])
    for tier in tiers:
        choice = _single(tier)
        if choice is not None:
            return choice
    return _Choice()


def _candidates(variable: str, binding: ConfigBinding | None) -> list[str]:
    if binding is None or binding.framework is not ConfigFramework.SPRING:
        return [variable]
    return list(dict.fromkeys([variable, *spring_candidates(binding.key)]))


def resolve_variable(
    variable: str,
    config: ServiceStaticConfig | None,
    environment: ServiceEnvironment | None,
    pipeline: PipelineVariables | None,
) -> ConfigResolution:
    """Return the address a service runs with for ``variable`` (see module doc)."""
    binding = config.binding(variable) if config else None
    framework = binding.framework if binding else ConfigFramework.ENV
    case_insensitive = framework is ConfigFramework.DOTNET
    connection_string = names_host(variable)

    def lookup(name: str) -> LayerValue:
        names = spring_candidates(name) if framework is ConfigFramework.SPRING else [name]
        return lookup_variable(names, environment, pipeline, config, case_insensitive)

    layer = lookup_variable(
        _candidates(variable, binding), environment, pipeline, config, case_insensitive
    )
    if layer.state is LayerState.OPAQUE:
        return ConfigResolution(outcome=ResolutionOutcome.OPAQUE, detail=layer.detail)
    if layer.state is LayerState.SET and layer.value is not None:
        endpoint = endpoint_in_value(layer.value, connection_string=connection_string)
        if not layer.from_image:
            return ConfigResolution(
                outcome=ResolutionOutcome.RUNTIME, endpoint=endpoint or layer.value
            )
        return _static(endpoint, StaticDefaultOrigin.IMAGE_ENV, config.image_file if config else None, None)
    if binding is None or not binding.defaults:
        return ConfigResolution(outcome=ResolutionOutcome.UNSET)

    profiles = active_profiles(framework, lookup)
    choice = choose_default(binding.defaults, profiles)
    if choice.ambiguous:
        return ConfigResolution(
            outcome=ResolutionOutcome.OPAQUE,
            detail="its defaults differ by configuration profile and the active profile is unknown",
        )
    if choice.default is None:
        return ConfigResolution(outcome=ResolutionOutcome.UNSET)
    default = choice.default
    evaluated = evaluate_template(default.template, lookup)
    if evaluated.state is LayerState.OPAQUE:
        return ConfigResolution(outcome=ResolutionOutcome.OPAQUE, detail=evaluated.detail)
    if evaluated.state is LayerState.UNSET or evaluated.value is None:
        return ConfigResolution(outcome=ResolutionOutcome.UNSET, detail=evaluated.detail)
    # A default without placeholders was stored as the address it names
    # (``storable_template``), possibly reduced to a bare host.
    stored_address = "${" not in default.template
    endpoint = endpoint_in_value(evaluated.value, connection_string=connection_string or stored_address)
    if evaluated.from_runtime:
        return ConfigResolution(outcome=ResolutionOutcome.RUNTIME, endpoint=endpoint or evaluated.value)
    origin = (
        StaticDefaultOrigin.CONFIG_FILE
        if default.source is StaticDefaultSource.CONFIG_FILE
        else StaticDefaultOrigin.CODE
    )
    return _static(endpoint, origin, default.file_path, default.profile)


def _static(
    endpoint: str | None, origin: StaticDefaultOrigin, file_path: str | None, profile: str | None
) -> ConfigResolution:
    """A static default's resolution; one naming no address or this host is UNSET."""
    where = file_path or "the image"
    if endpoint is None:
        return ConfigResolution(
            outcome=ResolutionOutcome.UNSET, detail=f"its default in {where} names no address"
        )
    if is_loopback_endpoint(endpoint):
        return ConfigResolution(
            outcome=ResolutionOutcome.UNSET,
            detail=f"it defaults to {endpoint} ({where}), this container itself",
        )
    return ConfigResolution(
        outcome=ResolutionOutcome.STATIC_DEFAULT,
        endpoint=endpoint,
        origin=origin,
        file_path=file_path or "Dockerfile",
        profile=profile,
    )

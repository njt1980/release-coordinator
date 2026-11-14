"""Plain-language meaning and the usual fix for each reason code.

One source for everything that explains a verdict to a person (the
assistant, the release briefing); docs/release-model.md is the reference.
"""

from typing import Final

from src.core.models import ReasonCode

REASON_EXPLANATIONS: Final[dict[ReasonCode, tuple[str, str]]] = {
    ReasonCode.POLICY_MISSING: (
        "The project has no release policy file.",
        "Add policies/<tenant>/<project>.yaml (reviewed in a pull request).",
    ),
    ReasonCode.POLICY_INVALID: (
        "The policy file is malformed or names another project.",
        "Fix the file; the error message names the field.",
    ),
    ReasonCode.BASELINE_UNAVAILABLE: (
        "There is no usable baseline snapshot for the project.",
        "Run onboarding (discovery) for the project.",
    ),
    ReasonCode.BASELINE_STALE: (
        "The baseline is older than the policy's max_baseline_age_hours.",
        "Re-run onboarding.",
    ),
    ReasonCode.BASELINE_SCOPE_MISMATCH: (
        "The baseline can't be shown to describe the policy's environment and cluster "
        "(other target, other Kubernetes API server, or other telemetry cluster filter).",
        "Re-run onboarding against the policy's cluster, or correct the policy.",
    ),
    ReasonCode.REQUIRED_SOURCE_MISSING: (
        "A required evidence source didn't succeed, or runtime evidence is weaker than the "
        "policy requires (e.g. live telemetry empty, stale or not covering the radius).",
        "Fix the source's access, wait for traffic, or list intentionally quiet services "
        "under exemptions.silent_services with a reason and expiry.",
    ),
    ReasonCode.UNMAPPED_FILES: (
        "Changed files belong to no registered service and aren't exempt.",
        "Declare the service in release-coord.yaml, or exempt the path in the policy.",
    ),
    ReasonCode.RUNTIME_UNOBSERVABLE: (
        "Live evidence is required, but a radius service talks only protocols the telemetry "
        "can't see (e.g. AMQP 0-9-1, Azure Service Bus), so not seeing it proves nothing.",
        "Instrument the service (OpenTelemetry or Application Insights auto-instrumentation), "
        "or accept the protocol under exemptions.unobservable with a reason and expiry.",
    ),
    ReasonCode.IDENTITY_UNLINKED: (
        "A service in the radius exists in source code but matches no running service, so "
        "its runtime calls and callers are unknown.",
        "Declare its deployed name in the repository's release-coord.yaml (a near name is "
        "suggested, never applied), or confirm it isn't deployed.",
    ),
    ReasonCode.DISCOVERY_GAP: (
        "An endpoint in the release radius matches no single service (IP, external host, "
        "unregistered or ambiguous service, unresolved variable), or a telemetry query failed.",
        "Register or declare the service, allow intended external calls as egress, or fix "
        "the configuration that hides the target.",
    ),
    ReasonCode.BASELINE_DRIFT: (
        "Live telemetry saw a dependency in the radius that the baseline lacks.",
        "Re-run onboarding so the baseline describes the running system.",
    ),
    ReasonCode.DEPENDENCY_CYCLE: (
        "No deployment order satisfies every dependency.",
        "Break the cycle or deploy the services together deliberately.",
    ),
    ReasonCode.COMPATIBILITY_UNVERIFIED: (
        "There is no evidence the contract change is compatible; contract analysis "
        "doesn't exist yet, so every release currently carries this.",
        "None yet: contract analysis (gap G8) is on the roadmap.",
    ),
    ReasonCode.DEMO_EVIDENCE: (
        "The evidence includes mock fixture data.",
        "Use real telemetry (TELEMETRY_SOURCE=azure) and re-run onboarding.",
    ),
    ReasonCode.MODEL_REJECTED: (
        "The advisory risk model rejected the release.",
        "Read its justification; it can block but never approve.",
    ),
    ReasonCode.REVISION_UNVERIFIED: (
        "The pull request's commits couldn't be verified with source control (a commit is "
        "missing, the base isn't an ancestor of the head, the changed files don't match the "
        "commits, or the head couldn't be scanned).",
        "Check the PR's base and head SHAs and the policy's source_control; rebase if the "
        "branch was rewritten.",
    ),
    ReasonCode.BASELINE_REVISION_MISMATCH: (
        "Commits between the baseline's scanned revision and the PR's base change services "
        "in the release, or the two revisions can't be related.",
        "Re-run onboarding so the baseline matches the base, or rebase the pull request.",
    ),
    ReasonCode.STATIC_COVERAGE_INCOMPLETE: (
        "Some source files in a service the release touches couldn't be read or understood "
        "(unsupported language, a dynamic configuration key, a client library with no "
        "address...), so dependencies they declare are invisible.",
        "Check the listed files by hand; exempt reviewed ones under "
        "exemptions.unscanned_files, or declare the dependency.",
    ),
    ReasonCode.REVIEW_UNAVAILABLE: (
        "The project's dependency review answers couldn't be read, so dependencies a "
        "coordinator declared may be missing from the deployment order.",
        "Check the review database (reviews/reviews.sqlite3) and re-run the assessment.",
    ),
    ReasonCode.ADVISORY_DISABLED: (
        "The evidence is complete, but the project policy keeps its data from the external "
        "model (external_ai: false), so no advisory risk review was made and nothing is "
        "approved automatically.",
        "Decide yourself from the evidence; or set external_ai: true in the policy if sending "
        "redacted PR content to the model is allowed.",
    ),
}

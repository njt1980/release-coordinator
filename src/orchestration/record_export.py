"""The decision record of one assessment, exported for archiving or review.

One self-contained file holds everything a quality reviewer needs without
the platform: the assessment exactly as stored (and its SHA-256, the value
every decision signed), the briefing derived from it, every decision on it
with its signature manifestation, the state of the whole hash chain when
exported, and whether the pinned baseline snapshot is still readable.
``export_sha256`` is taken over every other field (canonical JSON), so a
changed copy is detectable.

``render_print_view`` is the same content as a printable HTML page (no
scripts, everything escaped). Both are pure functions; the API reads the
stores. See docs/records-retention.md for how long records are kept.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Final

from pydantic import BaseModel, Field

from src.core.decision import DecisionRecord
from src.core.hashing import sha256_hex
from src.core.models import ReleaseAssessment
from src.orchestration.briefing import ReleaseBriefing, build_briefing

EXPORT_VERSION: Final[int] = 1


class ExportedDecision(BaseModel):
    """One decision as exported: the stored record, its meaning and
    signature line, whether the assessment is still the one signed, and
    whether a later decision supersedes it."""

    record: DecisionRecord
    meaning: str
    manifestation: str
    assessment_unchanged: bool
    superseded: bool


class ChainState(BaseModel):
    """The whole decision chain when exported (``problems`` empty when intact)."""

    records: int = Field(ge=0)
    last_hash: str
    intact: bool
    problems: list[str] = Field(default_factory=list)


class RecordExport(BaseModel):
    """The archivable record of one assessment and its decisions."""

    export_version: int = EXPORT_VERSION
    exported_at: datetime
    job_id: str = Field(min_length=1)
    tenant: str
    project_id: str
    assessment_sha256: str
    assessment: ReleaseAssessment
    briefing: ReleaseBriefing
    decisions: list[ExportedDecision]
    chain: ChainState
    baseline_snapshot_available: bool
    export_sha256: str = ""


def build_export(
    *,
    job_id: str,
    tenant: str,
    project_id: str,
    assessment: ReleaseAssessment,
    assessment_sha256: str,
    decisions: list[ExportedDecision],
    chain: ChainState,
    baseline_snapshot_available: bool,
    exported_at: datetime,
) -> RecordExport:
    """Assemble the export and seal it with ``export_sha256``."""
    export = RecordExport(
        exported_at=exported_at,
        job_id=job_id,
        tenant=tenant,
        project_id=project_id,
        assessment_sha256=assessment_sha256,
        assessment=assessment,
        briefing=build_briefing(assessment),
        decisions=decisions,
        chain=chain,
        baseline_snapshot_available=baseline_snapshot_available,
    )
    return export.model_copy(update={"export_sha256": export_digest(export)})


def export_digest(export: RecordExport) -> str:
    """SHA-256 over every field but ``export_sha256`` (canonical JSON)."""
    return sha256_hex(export.model_dump(mode="json", exclude={"export_sha256"}))


_STYLE: Final[str] = """
body{font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#0f172a;background:#fff;margin:0}
main{max-width:860px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px;border-bottom:1px solid #cbd5e1;padding-bottom:4px}
table{border-collapse:collapse;width:100%}td,th{text-align:left;vertical-align:top;padding:4px 8px 4px 0;border-bottom:1px solid #e2e8f0}
th{width:32%;color:#475569;font-weight:600}code,.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;overflow-wrap:anywhere}
.note{color:#475569;font-size:12px}.sig{border:1px solid #94a3b8;border-radius:6px;padding:8px 10px;margin:6px 0}
.warn{color:#9a3412;font-weight:600}ul{margin:4px 0;padding-left:20px}
@media print{main{padding:0}a{color:inherit;text-decoration:none}}
"""


def _row(label: str, value: str, mono: bool = False) -> str:
    cell = f'<span class="mono">{escape(value)}</span>' if mono else escape(value)
    return f"<tr><th>{escape(label)}</th><td>{cell}</td></tr>"


def _decision_html(decision: ExportedDecision) -> str:
    record = decision.record
    flags = []
    if decision.superseded:
        flags.append("superseded by a later decision")
    if not decision.assessment_unchanged:
        flags.append("the stored assessment no longer matches the one signed")
    flag = f' <span class="warn">({escape("; ".join(flags))})</span>' if flags else ""
    reason = f"<div>Reason: {escape(record.reason)}</div>" if record.reason else ""
    corrects = f'<div class="note">Corrects record {escape(record.corrects)}</div>' if record.corrects else ""
    return (
        f'<div class="sig"><strong>{escape(decision.manifestation)}</strong>{flag}{reason}{corrects}'
        f'<div class="note">Record {escape(record.record_id)}, sequence {record.sequence}, '
        f'hash <code>{escape(record.record_hash)}</code></div></div>'
    )


def render_print_view(export: RecordExport) -> str:
    """The export as a printable, script-free HTML page."""
    assessment, evidence, briefing = export.assessment, export.assessment.evidence, export.briefing
    chain = "intact" if export.chain.intact else "BROKEN: " + "; ".join(export.chain.problems)
    summary = "".join(
        [
            _row("Pull request", evidence.pr_id),
            _row("Repository", evidence.repository or "-"),
            _row("Head commit", evidence.head_sha or "-", mono=True),
            _row("Project", f"{export.tenant}/{export.project_id}"),
            _row("Tool verdict", "approved" if assessment.approved else "not approved"),
            _row("Actionable", "yes" if assessment.actionable else "no (evidence incomplete)"),
            _row("Reason codes", ", ".join(code.value for code in assessment.reason_codes) or "none"),
            _row("Baseline snapshot", evidence.baseline_snapshot_id or "-", mono=True),
            _row("Baseline still readable", "yes" if export.baseline_snapshot_available else "no"),
            _row("Policy", f"version {evidence.policy_version}" if evidence.policy_version else "-"),
            _row("Policy SHA-256", evidence.policy_sha256 or "-", mono=True),
            _row("Assessment SHA-256", export.assessment_sha256, mono=True),
        ]
    )
    groups = "".join(
        f"<li><strong>{escape(group.title)}</strong> ({escape(group.rank.value)}, {escape(group.origin.value)})"
        + (f"<ul>{''.join(f'<li>{escape(item)}</li>' for item in group.what_to_check[:8])}</ul>" if group.what_to_check else "")
        + "</li>"
        for group in briefing.groups
    )
    order = "".join(f"<li>{escape(step.service)}{' (changed)' if step.changed else ''}</li>" for step in briefing.deployment)
    decisions = "".join(_decision_html(d) for d in export.decisions) or "<p>No decision recorded.</p>"
    advisory = (
        f'<h2>Advisory opinion (AI-generated, not part of any signature)</h2><p>{escape(briefing.advisory.justification)}</p>'
        if briefing.advisory
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decision record {escape(evidence.pr_id)}</title><style>{_STYLE}</style></head>
<body><main>
<h1>Release decision record: {escape(evidence.pr_id)}</h1>
<p class="note">Exported {export.exported_at:%Y-%m-%d %H:%M:%S} UTC by Release Coordinator (export format {export.export_version}).
The assessment is advisory; the decisions below are the people's.</p>
<h2>Assessment</h2><table>{summary}</table>
<p><strong>{escape(briefing.headline)}</strong></p>
<h2>Decisions</h2>{decisions}
<h2>What the assessment found</h2><ul>{groups or '<li>Nothing to report.</li>'}</ul>
<h2>Deployment order</h2><ol>{order}</ol>
{advisory}
<h2>Integrity</h2><table>
{_row("Decision chain", f"{chain} ({export.chain.records} records)")}
{_row("Chain head hash", export.chain.last_hash, mono=True)}
{_row("Export SHA-256", export.export_sha256, mono=True)}
</table>
<p class="note">The export SHA-256 is taken over the JSON export's content; download the JSON for the
complete, machine-checkable record.</p>
</main></body></html>
"""

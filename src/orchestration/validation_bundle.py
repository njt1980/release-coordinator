"""The validation-support package of one release, exported as one bundle.

A quality reviewer validating the tool's use for one release needs more
than the decision record: which build of the tool made the assessment,
how well discovery is known to perform (the stored benchmark, held-out
apps reported apart from the apps it was tuned on), the dependency review
answers the assessment rested on, and the intended-use statement the
assessment must be read against. The bundle carries all of them with the
decision record (``record_export``), sealed by ``bundle_sha256`` over every
other field (canonical JSON).

``render_bundle_view`` is the same content as a printable HTML page (no
scripts, everything escaped; the intended-use statement is shown as its
source text). Both are pure functions; the API reads the stores and files.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Final

from pydantic import BaseModel, Field

from src.core.hashing import sha256_hex
from src.core.review import ReviewAnswer
from src.core.validation import BenchmarkResults, BenchmarkScores, IntendedUse, ToolVersion
from src.orchestration.record_export import ChainState, RecordExport, print_page, record_sections

BUNDLE_VERSION: Final[int] = 1


class ReviewEvidence(BaseModel):
    """The dependency review answers the assessment used or set aside.

    ``answers`` are the stored answers the assessment names (applied or
    needing re-review), with their reasons and anchors; ``missing`` the
    answer ids it names that the store no longer returns. ``chain`` is the
    whole review chain when exported. ``problem`` is set (and the rest
    empty) when the review store couldn't be read.
    """

    answers: list[ReviewAnswer] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    project_answers: int = Field(default=0, ge=0)
    chain: ChainState | None = None
    problem: str = ""


class ValidationBundle(BaseModel):
    """Everything needed to validate the tool's use for one release."""

    bundle_version: int = BUNDLE_VERSION
    exported_at: datetime
    tool: ToolVersion
    record: RecordExport
    review: ReviewEvidence
    benchmark: BenchmarkResults
    intended_use: IntendedUse
    bundle_sha256: str = ""


def referenced_answer_ids(record: RecordExport) -> list[str]:
    """The review answer ids the assessment names, in order, without repeats."""
    review = record.assessment.evidence.review
    if review is None:
        return []
    seen: dict[str, None] = {}
    for note in [*review.applied, *review.needs_rereview]:
        seen.setdefault(note.answer_id, None)
    return list(seen)


def review_evidence(
    record: RecordExport, project_answers: list[ReviewAnswer], chain: ChainState
) -> ReviewEvidence:
    """The stored answers ``record``'s assessment names, from the project's answers."""
    by_id = {answer.answer_id: answer for answer in project_answers}
    wanted = referenced_answer_ids(record)
    return ReviewEvidence(
        answers=[by_id[answer_id] for answer_id in wanted if answer_id in by_id],
        missing=[answer_id for answer_id in wanted if answer_id not in by_id],
        project_answers=len(project_answers),
        chain=chain,
    )


def build_bundle(
    *,
    record: RecordExport,
    tool: ToolVersion,
    review: ReviewEvidence,
    benchmark: BenchmarkResults,
    intended_use: IntendedUse,
    exported_at: datetime,
) -> ValidationBundle:
    """Assemble the bundle and seal it with ``bundle_sha256``."""
    bundle = ValidationBundle(
        exported_at=exported_at,
        tool=tool,
        record=record,
        review=review,
        benchmark=benchmark,
        intended_use=intended_use,
    )
    return bundle.model_copy(update={"bundle_sha256": bundle_digest(bundle)})


def bundle_digest(bundle: ValidationBundle) -> str:
    """SHA-256 over every field but ``bundle_sha256`` (canonical JSON)."""
    return sha256_hex(bundle.model_dump(mode="json", exclude={"bundle_sha256"}))


def _row(label: str, value: str, mono: bool = False) -> str:
    cell = f'<span class="mono">{escape(value)}</span>' if mono else escape(value)
    return f"<tr><th>{escape(label)}</th><td>{cell}</td></tr>"


def _ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _count(value: int | None) -> str:
    return "-" if value is None else str(value)


_SCORE_HEAD: Final[str] = (
    "<tr><th>Apps</th><th>Inventory P / R</th><th>Dependencies P / R</th>"
    "<th>Silent misses</th><th>Flag precision</th></tr>"
)


def _score_row(label: str, scores: BenchmarkScores | None) -> str:
    if scores is None:
        return f"<tr><td>{escape(label)}</td><td colspan=\"4\">not recorded</td></tr>"
    return (
        f"<tr><td>{escape(label)}</td>"
        f"<td>{_ratio(scores.inventory_precision)} / {_ratio(scores.inventory_recall)}</td>"
        f"<td>{_ratio(scores.manifest_precision)} / {_ratio(scores.manifest_recall)}</td>"
        f"<td>{_count(scores.missed_silent)}</td><td>{_ratio(scores.flag_precision)}</td></tr>"
    )


def _benchmark_html(benchmark: BenchmarkResults) -> str:
    if not benchmark.available:
        return f'<p class="warn">The benchmark results were not available: {escape(benchmark.problem)}</p>'
    held_out = [app.name for app in benchmark.apps if app.split == "held_out"]
    rows = _score_row("All apps", benchmark.pooled) + _score_row(
        f"Held-out apps ({', '.join(held_out) or 'none'})", benchmark.pooled_held_out
    )
    per_app = "".join(_score_row(f"{app.name} ({app.split})", app.scores) for app in benchmark.apps)
    return (
        '<p class="note">Discovery measured against hand-built ground truth for public microservice apps. '
        "Held-out apps were never used to tune the scanner; the other apps flatter it. "
        "Dependencies P / R is precision and recall against the apps' deployment manifests; "
        "a silent miss is a real dependency the scan neither found nor flagged.</p>"
        f"<table>{_SCORE_HEAD}{rows}</table>"
        f"<h3>Per app</h3><table>{_SCORE_HEAD}{per_app}</table>"
        f'<p class="note">From {escape(benchmark.source_file)}, SHA-256 <code>{escape(benchmark.sha256)}</code></p>'
    )


def _answer_html(answer: ReviewAnswer) -> str:
    reason = f"<div>Reason: {escape(answer.reason)}</div>" if answer.reason else ""
    link = f"<div>Linked to {escape(answer.linked_runtime_name)}</div>" if answer.linked_runtime_name else ""
    return (
        f'<div class="sig"><strong>{escape(answer.verdict.value)}</strong> on '
        f"<code>{escape(answer.subject.key)}</code>{reason}{link}"
        f'<div class="note">{escape(answer.reviewer)}, {answer.answered_at:%Y-%m-%d %H:%M:%S} UTC, '
        f"on baseline {escape(answer.snapshot_id)}; answer {escape(answer.answer_id)}</div></div>"
    )


def _review_html(review: ReviewEvidence) -> str:
    if review.problem:
        return f'<p class="warn">The dependency review could not be read: {escape(review.problem)}</p>'
    answers = "".join(_answer_html(answer) for answer in review.answers) or (
        "<p>The assessment used no dependency review answers.</p>"
    )
    missing = (
        f'<p class="warn">Named by the assessment but no longer stored: {escape(", ".join(review.missing))}</p>'
        if review.missing
        else ""
    )
    chain = ""
    if review.chain is not None:
        state = "intact" if review.chain.intact else "BROKEN: " + "; ".join(review.chain.problems)
        chain = (
            f"<table>{_row('Review chain', f'{state} ({review.chain.records} answers)')}"
            f"{_row('Review chain head hash', review.chain.last_hash, mono=True)}</table>"
        )
    return (
        f"{answers}{missing}<p class=\"note\">The project has {review.project_answers} review "
        f"answer(s) in all.</p>{chain}"
    )


def render_bundle_view(bundle: ValidationBundle) -> str:
    """The bundle as a printable, script-free HTML page."""
    pr_id = bundle.record.assessment.evidence.pr_id
    intended = (
        f"<pre>{escape(bundle.intended_use.text)}</pre>"
        f'<p class="note">From {escape(bundle.intended_use.source_file)}, SHA-256 '
        f"<code>{escape(bundle.intended_use.sha256)}</code></p>"
        if bundle.intended_use.available
        else f'<p class="warn">The intended-use statement was not available: {escape(bundle.intended_use.problem)}</p>'
    )
    body = f"""<h1>Validation package: {escape(pr_id)}</h1>
<p class="note">Exported {bundle.exported_at:%Y-%m-%d %H:%M:%S} UTC by Release Coordinator (bundle format {bundle.bundle_version}).
The assessment is advisory; the decisions are the people's. Read it against the intended-use statement below.</p>
<h2>Tool</h2><table>
{_row("Tool version", bundle.tool.version, mono=True)}
{_row("Version known from", bundle.tool.source)}
{_row("Bundle SHA-256", bundle.bundle_sha256, mono=True)}
</table>
{record_sections(bundle.record)}
<h2>Dependency review answers used</h2>{_review_html(bundle.review)}
<h2>Discovery benchmark</h2>{_benchmark_html(bundle.benchmark)}
<h2>Intended use</h2>{intended}
<p class="note">The bundle SHA-256 is taken over the JSON bundle's content; download the JSON for the
complete, machine-checkable package.</p>"""
    return print_page(f"Validation package {pr_id}", body)

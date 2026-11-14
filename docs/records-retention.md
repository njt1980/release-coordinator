# Records and retention

What Release Coordinator stores about release decisions, how long it keeps it, and how to archive it.
This is the platform's side. How long your organization must keep release records (for example for
the life of a medical device plus a fixed period) is set by your quality system, not by the platform.

## What is stored

| Record | Where (under `RC_DATA_DIR`, else the repository root) | Can it change? |
|---|---|---|
| Decision records (who decided what, when, why, on which assessment) | `decisions/decisions.sqlite3` | No. Append-only: database triggers refuse UPDATE and DELETE, and every record carries a SHA-256 hash chained to the one before. A correction is a new record that names the one it corrects. |
| Dependency review answers | `reviews/reviews.sqlite3` | No. Same append-only design and hash chain. |
| Assessments | `assessments/<job_id>.json`, **only when `RC_DATA_DIR` is set**; otherwise in memory and lost on restart | Written once per assessment; never rewritten. |
| Discovery baselines (snapshots) | `baselines/<tenant>/<project>/<snapshot>.json` | Written once; a new discovery writes a new snapshot. |
| Project policies | `policies/<tenant>/<project>.yaml` | Edited by people; each assessment records the version and SHA-256 of the policy it used. |

## Retention

- **Set `RC_DATA_DIR`.** Without it, assessments live only in the server's memory; a decision record
  survives a restart, but the assessment it signed doesn't (the export and `assessment_unchanged`
  can then no longer be produced). The showcase sets it.
- **The platform deletes nothing.** No record, assessment, snapshot or answer is deleted or pruned
  automatically, so every decision keeps the assessment, the baseline snapshot and the policy hash it
  rests on for as long as the data directory exists.
- **Retention is the storage's.** On the Azure showcase the data lives on an Azure Files share. Turn on
  share soft delete and backup (Azure Backup for Azure Files) with the retention your quality system
  requires. Locally, back up `RC_DATA_DIR`.
- **Archive with the export.** Each assessment's record exports as one self-contained JSON file:
  `GET /api/v1/webhooks/pr-assessment/{job_id}/record`, or **Download the record (JSON)** on the
  Assessments page. It holds the assessment, its SHA-256 (the value every decision signed), the
  briefing, every decision with its signature line, the decision chain's state, and whether the
  baseline snapshot was still readable. `export_sha256` is taken over all of it, so a changed copy is
  detectable. Store the file in your document management system with the release.
- **Print view.** `?format=html` (or **Print view of the record**) shows the same content as a
  printable page, for a signature binder or a review meeting.
- **Validation package per release.** `GET /api/v1/webhooks/pr-assessment/{job_id}/validation-bundle`
  (JSON, or `?format=html` to print; links under the decisions on the Assessments page) adds to the
  record everything needed to validate the tool's use for that release: the tool version (the
  showcase image's commit, `RC_TOOL_VERSION`; locally the git commit), the stored dependency review
  answers the assessment used (with reasons, and the review chain's state), the discovery benchmark
  as stored with this build (held-out apps reported apart from the apps the scanner was tuned on),
  and the intended-use statement (`docs/intended-use.md`) with its SHA-256. `bundle_sha256` is taken
  over all of it. A part that can't be read is reported in the bundle, not left out silently.
- **Check integrity.** `GET /api/v1/decisions/verify` walks the whole decision chain and reports any
  record whose hash, link or position doesn't check out.

## Limits

- Signatures are electronic signatures only behind the platform's Entra sign-in. Locally, the name is
  typed and the record says so.
- Deleting the data directory or the file share deletes the records: protect them with the storage's
  own access control, soft delete and backup.
- The export is not itself signed with a certificate. Its hash shows a change, but nothing proves who
  made the copy.

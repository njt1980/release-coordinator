import { useEffect, useState } from "react";

const API = "/api/v1";

const VERDICTS = [
  { value: "approve", label: "Approve", meaning: "Approved for release" },
  { value: "further_analysis", label: "Further analysis", meaning: "Further analysis requested before release" },
  { value: "decline", label: "Decline", meaning: "Declined for release" },
];

const VERDICT_STYLE = {
  approve: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  further_analysis: "bg-amber-50 text-amber-800 ring-amber-200",
  decline: "bg-red-50 text-red-800 ring-red-200",
};

async function readJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? "");
    throw new Error(detail || `Request failed with status ${response.status}`);
  }
  return body;
}

function DecisionLine({ view }) {
  const { record } = view;
  return (
    <li className={`rounded-xl px-4 py-3 ring-1 ${view.superseded ? "bg-white opacity-70 ring-slate-200" : "bg-white ring-slate-300"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${VERDICT_STYLE[record.verdict]}`}>
          {view.meaning}
        </span>
        <span className="rounded-full bg-white px-2 py-0.5 text-xs font-medium text-slate-700 ring-1 ring-slate-300">
          Human
        </span>
        {view.superseded && <span className="text-xs text-slate-500">superseded by a later decision</span>}
        {record.corrects && <span className="text-xs text-slate-500">corrects an earlier decision</span>}
      </div>
      {/* The signature manifestation: meaning, printed name, UTC time. Always shown in full. */}
      <p className="mt-2 text-sm text-slate-900 [overflow-wrap:anywhere]">{view.manifestation}</p>
      {record.reason && <p className="mt-1 text-sm text-slate-600 [overflow-wrap:anywhere]">Reason: {record.reason}</p>}
      <p className="mt-1 font-mono text-xs text-slate-500 [overflow-wrap:anywhere]">
        head {record.head_sha.slice(0, 12)} · baseline {record.baseline_snapshot_id ?? "none"} · policy{" "}
        {record.policy_version != null ? `v${record.policy_version}` : "none"} · assessment {record.assessment_sha256.slice(0, 12)} · record{" "}
        {record.record_hash.slice(0, 12)}
      </p>
      {!view.assessment_unchanged && (
        <p role="alert" className="mt-2 rounded-md bg-red-50 px-2 py-1 text-xs text-red-700">
          The assessment stored now is not the one this decision was signed on.
        </p>
      )}
    </li>
  );
}

/** A person's release decision on one assessment, recorded with its signature. */
export default function DecisionPanel({ jobId, toolApproved }) {
  const [views, setViews] = useState(null);
  const [signer, setSigner] = useState(null);
  const [verdict, setVerdict] = useState("");
  const [reason, setReason] = useState("");
  const [typedName, setTypedName] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetch(`${API}/webhooks/pr-assessment/${encodeURIComponent(jobId)}/decisions`).then(readJson),
      fetch(`${API}/decisions/signer`).then(readJson),
    ])
      .then(([decisions, who]) => {
        if (cancelled) return;
        setViews(decisions);
        setSigner(who);
      })
      .catch((caught) => !cancelled && setError(caught.message));
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  const current = views?.length ? views[views.length - 1] : null;
  const typed = signer?.source === "typed";
  const name = typed ? typedName.trim() : signer?.name;
  const needsReason = verdict && (verdict !== "approve" || !toolApproved);
  const ready = verdict && name && (!needsReason || reason.trim()) && signer?.can_decide;
  const meaning = VERDICTS.find((v) => v.value === verdict)?.meaning;

  const sign = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = { verdict, reason: reason.trim(), signer_name: typed ? typedName.trim() : "" };
      if (current) body.corrects = current.record.record_id;
      const response = await fetch(`${API}/webhooks/pr-assessment/${encodeURIComponent(jobId)}/decisions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setViews(await readJson(response));
      setVerdict("");
      setReason("");
      setConfirming(false);
    } catch (caught) {
      setError(caught.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Release decision" className="rounded-2xl bg-white p-4 ring-1 ring-slate-200 sm:p-5">
      <h4 className="text-sm font-semibold text-slate-900">Your decision</h4>
      <p className="text-xs text-slate-500">
        Recorded with your name, the UTC time and its meaning, tied to this exact assessment. Records are never
        changed; a later decision supersedes an earlier one and both stay visible.
      </p>

      {views?.length > 0 && (
        <ol className="mt-3 space-y-2" aria-label="Decisions recorded">
          {views.map((view) => (
            <DecisionLine key={view.record.record_id} view={view} />
          ))}
        </ol>
      )}
      <p className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm">
        <a
          href={`${API}/webhooks/pr-assessment/${jobId}/record?format=html`}
          target="_blank"
          rel="noopener noreferrer"
          className="font-medium text-brand-700 hover:underline"
        >
          Print view of the record
        </a>
        <a href={`${API}/webhooks/pr-assessment/${jobId}/record`} className="font-medium text-brand-700 hover:underline">
          Download the record (JSON)
        </a>
      </p>

      {signer && !signer.can_decide ? (
        <p className="mt-3 text-sm text-slate-600">{signer.why}</p>
      ) : (
        <div className="mt-4 space-y-3">
          <fieldset>
            <legend className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              {current ? "Record a new decision (supersedes the current one)" : "Decision"}
            </legend>
            <div className="mt-1 flex flex-wrap gap-2">
              {VERDICTS.map((option) => (
                <label
                  key={option.value}
                  className={`cursor-pointer rounded-lg px-3 py-1.5 text-sm ring-1 ${
                    verdict === option.value ? VERDICT_STYLE[option.value] : "bg-white text-slate-700 ring-slate-300"
                  }`}
                >
                  <input
                    type="radio"
                    name={`verdict-${jobId}`}
                    value={option.value}
                    checked={verdict === option.value}
                    onChange={() => {
                      setVerdict(option.value);
                      setConfirming(false);
                    }}
                    className="sr-only"
                  />
                  {option.label}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="block">
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Reason{needsReason ? " (required)" : " (optional)"}
            </span>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={2}
              maxLength={4000}
              placeholder={
                verdict === "approve" && !toolApproved
                  ? "The assessment did not approve this release; say why you do."
                  : "What you checked, or what must happen next."
              }
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
            />
          </label>
          {typed && (
            <label className="block sm:w-72">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Your name</span>
              <input
                value={typedName}
                onChange={(e) => setTypedName(e.target.value)}
                maxLength={200}
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
              <span className="mt-1 block text-xs text-slate-500">
                Local pilot without sign-in: recorded as a typed name, not an electronic signature.
              </span>
            </label>
          )}
          {!confirming ? (
            <button
              type="button"
              disabled={!ready}
              onClick={() => setConfirming(true)}
              className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Review and sign
            </button>
          ) : (
            <div className="rounded-xl bg-slate-50 p-3 ring-1 ring-slate-200">
              <p className="text-sm text-slate-900 [overflow-wrap:anywhere]">
                You are about to record: <strong>{meaning}</strong>, signed by <strong>{name}</strong>
                {signer?.identifier && !typed ? ` <${signer.identifier}>` : ""}, at the current UTC time, for this
                assessment of this commit.
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={sign}
                  className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-50"
                >
                  {busy ? "Recording…" : "Sign and record"}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirming(false)}
                  className="rounded-lg px-4 py-2 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-white"
                >
                  Back
                </button>
              </div>
            </div>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 [overflow-wrap:anywhere]">
          {error}
        </p>
      )}
    </section>
  );
}

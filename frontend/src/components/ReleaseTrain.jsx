import { useState } from "react";

const API = "/api/v1/release-trains";

const STATUS = {
  approved: { mark: "✓", className: "bg-emerald-50 ring-emerald-200 text-emerald-800" },
  decision_needed: { mark: "?", className: "bg-amber-50 ring-amber-200 text-amber-800" },
  blocked: { mark: "✕", className: "bg-red-50 ring-red-200 text-red-800" },
};

function memberVerdict(member) {
  if (member.approved) return "Approved";
  return member.actionable ? "Decision needed" : "Blocked";
}

function TrainBriefing({ train }) {
  const tone = STATUS[train.status] ?? STATUS.blocked;
  return (
    <div className="mt-4 space-y-4">
      <div className={`rounded-xl px-4 py-3 ring-1 ${tone.className}`}>
        <p className="text-sm font-semibold">
          {tone.mark} {train.headline}
        </p>
        <p className="mt-1 text-xs text-slate-600">
          {train.title} · {train.tenant}/{train.project_id} · worst member wins. The platform
          advises; the decision is yours.
        </p>
      </div>

      {train.issues.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Issues only the combination shows
          </h4>
          <ul className="mt-1 space-y-1">
            {train.issues.map((issue) => (
              <li key={issue.kind} className="rounded-md bg-white px-3 py-2 text-sm text-slate-700 ring-1 ring-slate-200">
                <span className="mr-2 font-mono text-xs text-red-700">{issue.kind}</span>
                {issue.detail}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Deployment order</h4>
        <ol className="mt-1 flex flex-wrap gap-1.5 text-xs">
          {train.deployment_sequence.map((service, index) => {
            const changed = train.services_to_deploy.includes(service);
            const cyclic = train.cyclic_services.includes(service);
            return (
              <li
                key={service}
                className={`rounded-md px-2 py-1 ring-1 ${
                  changed ? "bg-white font-medium text-slate-900 ring-brand-300" : "bg-slate-100 text-slate-600 ring-slate-200"
                }`}
                title={changed ? "Changed by a pull request in this train" : "Unchanged, in the release radius"}
              >
                {index + 1}. {service.split("/").pop()}
                {changed && <span className="ml-1 text-brand-600">(changed)</span>}
                {cyclic && <span className="ml-1 text-red-700">(cycle)</span>}
              </li>
            );
          })}
        </ol>
        {!train.safe_order_established && (
          <p className="mt-1 text-xs text-slate-500">
            Order not proven safe: it follows the declared change types, and every member must have
            verified compatibility.
          </p>
        )}
      </div>

      {(train.stale_consumers.length > 0 || train.consumers_changed_in_train.length > 0) && (
        <div className="text-sm text-slate-700">
          {train.stale_consumers.length > 0 && (
            <p>
              Consumers to update before a breaking change, not in this train:{" "}
              <span className="font-medium">{train.stale_consumers.join(", ")}</span>
            </p>
          )}
          {train.consumers_changed_in_train.length > 0 && (
            <p>
              Consumers another pull request in this train changes (check it makes them compatible):{" "}
              <span className="font-medium">{train.consumers_changed_in_train.join(", ")}</span>
            </p>
          )}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="py-1 pr-3 font-medium">Pull request</th>
              <th className="py-1 pr-3 font-medium">Verdict</th>
              <th className="py-1 pr-3 font-medium">Change</th>
              <th className="py-1 font-medium">Reasons</th>
            </tr>
          </thead>
          <tbody className="text-slate-700">
            {train.members.map((member) => (
              <tr key={member.job_id} className="border-t border-slate-200 align-top">
                <td className="py-1.5 pr-3">
                  <span className="font-medium">{member.pr_id}</span>
                  {member.repository && <span className="block text-slate-500">{member.repository}</span>}
                </td>
                <td className="py-1.5 pr-3">{memberVerdict(member)}</td>
                <td className="py-1.5 pr-3">{member.change_type ?? "unknown"}</td>
                <td className="py-1.5 font-mono">{member.reason_codes.join(", ") || "none"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// Pick completed assessments of one project and assess them as one release.
export default function ReleaseTrain({ items }) {
  const completed = (items ?? []).filter((item) => item.status === "completed");
  const [picked, setPicked] = useState([]);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [train, setTrain] = useState(null);

  if (completed.length < 2) return null;

  const toggle = (jobId) =>
    setPicked((current) => (current.includes(jobId) ? current.filter((id) => id !== jobId) : [...current, jobId]));

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_ids: picked, title }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof data.detail === "string" ? data.detail : `status ${response.status}`;
        throw new Error(detail);
      }
      setTrain(data);
    } catch (caught) {
      setError(caught.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      aria-label="Release train"
      className="mt-8 rounded-2xl bg-white p-6 shadow-sm ring-1 ring-slate-200 sm:p-8"
    >
      <h2 className="text-lg font-semibold text-slate-900">Release train</h2>
      <p className="mt-1 text-sm text-slate-600">
        Several pull requests shipping together, possibly from several repositories: pick their
        completed assessments (one project) to see one combined radius, order and verdict.
      </p>
      <form onSubmit={submit} className="mt-4 space-y-3">
        <fieldset>
          <legend className="sr-only">Assessments in the train</legend>
          <ul className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {completed.map((item) => (
              <li key={item.job_id}>
                <label className="flex items-start gap-2 rounded-md px-2 py-1.5 text-sm ring-1 ring-slate-200 hover:bg-slate-50">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={picked.includes(item.job_id)}
                    onChange={() => toggle(item.job_id)}
                  />
                  <span className="min-w-0">
                    <span className="font-medium text-slate-900">{item.pr_id}</span>
                    <span className="ml-1 text-xs text-slate-500">
                      {item.tenant}/{item.project_id}
                    </span>
                    {item.headline && <span className="block truncate text-xs text-slate-500">{item.headline}</span>}
                  </span>
                </label>
              </li>
            ))}
          </ul>
        </fieldset>
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            type="text"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Title (optional), e.g. Sprint 12 release"
            maxLength={200}
            className="min-w-0 flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={busy || picked.length < 2}
            className="rounded-md bg-brand-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {busy ? "Combining…" : `Assess ${picked.length || ""} as one release`}
          </button>
        </div>
      </form>
      {error && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          Could not build the release train: {error}
        </p>
      )}
      {train && <TrainBriefing train={train} />}
    </section>
  );
}

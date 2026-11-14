import { useState } from "react";

const STRENGTH = {
  live_and_declared: { label: "Live + code/cluster", tone: "bg-emerald-100 text-emerald-800" },
  live: { label: "Live only", tone: "bg-sky-100 text-sky-800" },
  declared: { label: "Code/cluster only", tone: "bg-slate-100 text-slate-700" },
  human: { label: "Declared by a person", tone: "bg-violet-100 text-violet-800" },
};

const CHANGE = {
  service_added: "First found",
  service_removed: "No longer found",
  dependency_added: "New dependency",
  dependency_removed: "Dependency gone",
  image_changed: "Image",
  source_revision_changed: "Repository commit",
  config_changed: "Configuration (names only)",
  contracts_changed: "Contracts",
};

function Changes({ changes }) {
  if (!changes.length) return <span className="text-slate-400">no recorded change</span>;
  return (
    <ul className="space-y-1">
      {changes.map((change, index) => (
        <li key={index} className="[overflow-wrap:anywhere]">
          <span className="font-medium text-amber-800">{CHANGE[change.kind] ?? change.kind}:</span> {change.detail}
          <span className="block text-slate-400">in {change.snapshot_id}</span>
        </li>
      ))}
    </ul>
  );
}

/** Where to look first if a service fails: its dependencies by hop and evidence, and what changed. Advisory. */
export default function RootCause({ url }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(url);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail ?? `Request failed with status ${response.status}`);
      setReport(body);
    } catch (caught) {
      setReport(null);
      setError(caught.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <section aria-label="If it fails" className="mt-6 rounded-xl bg-white p-4 ring-1 ring-slate-200">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">If it fails: where to look first</h2>
          <p className="text-xs text-slate-500">
            <span className="rounded bg-amber-100 px-1.5 py-0.5 font-semibold text-amber-800">Advisory</span> What it
            depends on, by hop and evidence, with what changed across recent baselines.
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="self-start rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 sm:self-auto"
        >
          {loading ? "Loading…" : report ? "Refresh" : "Show"}
        </button>
      </div>

      {error && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}

      {report && (
        <div className="mt-4 space-y-4 text-sm">
          <p className="text-xs text-slate-500">{report.advisory}</p>
          {report.notes.map((note) => (
            <p key={note} className="text-xs text-amber-800">
              {note}
            </p>
          ))}
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{report.name} itself</h3>
            <div className="mt-1 text-xs">
              <Changes changes={report.service_changes} />
            </div>
          </div>

          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              What it depends on ({report.upstreams.length}
              {report.left_out ? `, ${report.left_out} more left out` : ""})
            </h3>
            <p className="text-xs text-slate-500">{report.ranking_basis}</p>
            {report.upstreams.length === 0 ? (
              <p className="mt-1 text-slate-500">No service dependencies in the newest baseline.</p>
            ) : (
              <div className="mt-2 overflow-x-auto">
                <table className="min-w-full text-left text-sm">
                  <thead className="text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th scope="col" className="px-2 py-1">Hop</th>
                      <th scope="col" className="px-2 py-1">Service</th>
                      <th scope="col" className="px-2 py-1">Evidence</th>
                      <th scope="col" className="px-2 py-1">Recorded changes</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 align-top">
                    {report.upstreams.map((upstream) => {
                      const strength = STRENGTH[upstream.strength] ?? { label: upstream.strength, tone: "bg-slate-100" };
                      return (
                        <tr key={upstream.service_id}>
                          <td className="px-2 py-2 text-slate-500">{upstream.hops}</td>
                          <td className="px-2 py-2">
                            <span className="font-medium text-slate-900">{upstream.name}</span>
                            <span className="block text-xs text-slate-500">
                              {upstream.namespace} · via {upstream.via.join(", ")}
                            </span>
                          </td>
                          <td className="px-2 py-2">
                            <span className={`rounded-full px-2 py-0.5 text-xs ${strength.tone}`}>{strength.label}</span>
                          </td>
                          <td className="px-2 py-2 text-xs">
                            <Changes changes={upstream.changes} />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {report.unresolved.length > 0 && (
            <div>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Endpoints outside the map</h3>
              <ul className="mt-1 space-y-0.5 text-xs text-slate-700">
                {report.unresolved.map((item) => (
                  <li key={item.reference} className="[overflow-wrap:anywhere]">
                    <span className="font-mono">{item.reference}</span> (hop {item.hops}, from {item.from_services.join(", ")})
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Recent PR assessments touching these</h3>
            {report.recent_pull_requests.length === 0 ? (
              <p className="mt-1 text-xs text-slate-500">None in this server's assessments.</p>
            ) : (
              <ul className="mt-1 space-y-0.5 text-xs text-slate-700">
                {report.recent_pull_requests.map((pr) => (
                  <li key={pr.job_id}>
                    <span className="font-medium">PR {pr.pr_id}</span> changed {pr.services.join(", ")}
                    {pr.headline ? ` · ${pr.headline}` : ""}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <p className="text-xs text-slate-400">Compared baselines: {report.baselines_compared.join(", ")}</p>
        </div>
      )}
    </section>
  );
}

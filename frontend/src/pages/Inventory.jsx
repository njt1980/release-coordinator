import { useCallback, useEffect, useState } from "react";
import Nav from "../components/Nav.jsx";
import { ROUTES } from "../lib/router.js";

const STORAGE_KEY = "rc-review-scope"; // shares the project scope with the Review page

const SOURCES = {
  STATIC_CODE: "Code",
  K8S_MANIFEST: "Cluster",
  RUNTIME_TELEMETRY: "Telemetry",
  HUMAN_DECLARED: "Declared",
};

function loadSaved() {
  try {
    return { tenant: "default", projectId: "default", ...JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") };
  } catch {
    return { tenant: "default", projectId: "default" };
  }
}

function saveScope(scope) {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...saved, tenant: scope.tenant, projectId: scope.projectId }));
  } catch {
    // Storage unavailable: the scope just isn't remembered.
  }
}

function seen(value) {
  if (!value) return "never";
  return `${value.slice(0, 10)} ${value.slice(11, 16)} UTC`;
}

function Coverage({ row }) {
  if (row.unscanned_files === null || row.unscanned_files === undefined) {
    return <span className="text-slate-400">no code scanned</span>;
  }
  if (row.unscanned_files === 0) return <span>{row.files_scanned} files, all read</span>;
  return (
    <span className="text-amber-800">
      {row.unscanned_files} of {row.files_scanned + row.unscanned_files} unread
    </span>
  );
}

/** Every service in the newest baseline, most critical first, with what the platform knows about it. */
export default function Inventory() {
  const [scope, setScope] = useState(loadSaved);
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const base = `/api/v1/projects/${encodeURIComponent(scope.tenant)}/${encodeURIComponent(scope.projectId)}/inventory`;

  const load = useCallback(async () => {
    setError(null);
    saveScope(scope);
    try {
      const response = await fetch(base);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail ?? `Request failed with status ${response.status}`);
      setReport(body);
    } catch (caught) {
      setReport(null);
      setError(caught.message);
    }
  }, [base, scope]);

  useEffect(() => {
    load();
    // Load once for the remembered scope; later loads come from the form.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const field = (name, label) => (
    <label className="block sm:w-48">
      <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</span>
      <input
        value={scope[name]}
        onChange={(e) => setScope({ ...scope, [name]: e.target.value })}
        className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
      />
    </label>
  );

  return (
    <>
      <Nav path={ROUTES.inventory} tone="light" />
      <main className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
        <header className="mb-8 max-w-2xl">
          <p className="text-sm font-semibold uppercase tracking-wider text-brand-600">Inventory</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">Every service, and how much rests on it</h1>
          <p className="mt-3 text-sm leading-relaxed text-slate-600">
            What the newest baseline knows about each service: where it lives, which sources found it, what depends
            on it, whether telemetry sees it and how completely its code was read.
          </p>
        </header>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            load();
          }}
          className="mb-6 flex flex-col gap-3 rounded-xl bg-white p-4 ring-1 ring-slate-200 sm:flex-row sm:items-end"
        >
          {field("tenant", "Tenant")}
          {field("projectId", "Project")}
          <button type="submit" className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700">
            Load
          </button>
          {report && (
            <a href={`${base}?format=csv`} className="text-sm font-medium text-brand-600 hover:text-brand-700 sm:ml-auto sm:self-center">
              Download CSV
            </a>
          )}
        </form>

        {error && (
          <p role="alert" className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}

        {report && (
          <>
            <p className="mb-1 text-sm text-slate-600">
              Baseline <span className="font-mono [overflow-wrap:anywhere]">{report.snapshot_id}</span>: {report.rows.length}{" "}
              services.
            </p>
            <p className="mb-4 text-xs text-slate-500">
              <span className="font-semibold">Computed.</span> {report.criticality_basis} Business importance isn't known
              to the platform.
            </p>
            <div className="overflow-x-auto rounded-xl bg-white ring-1 ring-slate-200">
              <table className="min-w-full text-left text-sm">
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-3 py-2">#</th>
                    <th scope="col" className="px-3 py-2">Service</th>
                    <th scope="col" className="px-3 py-2 text-right" title="Services that depend on it, directly or through others">Depend on it</th>
                    <th scope="col" className="px-3 py-2 text-right">Callers / callees</th>
                    <th scope="col" className="px-3 py-2 text-right" title="Its dependencies that resolve to no service">Unresolved</th>
                    <th scope="col" className="px-3 py-2">Found by</th>
                    <th scope="col" className="px-3 py-2">Telemetry last saw it</th>
                    <th scope="col" className="px-3 py-2">Code read</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {report.rows.map((row) => (
                    <tr key={row.service_id} className="align-top">
                      <td className="px-3 py-2 text-slate-500">{row.criticality_rank}</td>
                      <td className="px-3 py-2">
                        <span className="font-medium text-slate-900">{row.name}</span>
                        <span className="block text-xs text-slate-500">
                          {row.namespace}
                          {row.repository ? ` · ${row.repository}${row.service_root ? `/${row.service_root}` : ""}` : ""}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right font-medium text-slate-900">{row.dependents}</td>
                      <td className="px-3 py-2 text-right text-slate-700">
                        {row.callers} / {row.callees}
                      </td>
                      <td className={`px-3 py-2 text-right ${row.unresolved_targets ? "text-amber-800" : "text-slate-400"}`}>
                        {row.unresolved_targets}
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex flex-wrap gap-1">
                          {row.found_by.map((source) => (
                            <span key={source} className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-700">
                              {SOURCES[source] ?? source}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td className={`whitespace-nowrap px-3 py-2 text-xs ${row.runtime_last_seen ? "text-slate-700" : "text-slate-400"}`}>
                        {seen(row.runtime_last_seen)}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs">
                        <Coverage row={row} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </main>
    </>
  );
}

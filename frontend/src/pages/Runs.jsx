import { useEffect, useState } from "react";
import Nav from "../components/Nav.jsx";
import RunLog, { RunStateBadge } from "../components/RunLog.jsx";
import { ROUTES } from "../lib/router.js";

const REFRESH_MS = 3000;

const when = (iso) =>
  new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

/** Recent onboarding and PR assessment runs, each with its live log. */
export default function Runs() {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    const load = async () => {
      try {
        const response = await fetch("/api/runs");
        if (!response.ok) throw new Error(`Request failed with status ${response.status}`);
        const data = await response.json();
        if (cancelled) return;
        setRuns(data);
        setError(null);
        setSelected((current) => current ?? data[0]?.run_id ?? null);
      } catch (caught) {
        if (!cancelled) setError(caught.message);
      }
      if (!cancelled) timer = setTimeout(load, REFRESH_MS);
    };
    load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  return (
    <>
      <Nav path={ROUTES.runs} tone="light" />
      <main className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
        <header className="mb-8 max-w-2xl">
          <p className="text-sm font-semibold uppercase tracking-wider text-brand-600">Runs</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">
            What was checked, and what it found
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-slate-600">
            Every onboarding and pull request assessment, step by step, as it happens. Logs
            are kept in memory and cleared when the backend restarts; each baseline keeps the
            log of the onboarding that built it.
          </p>
        </header>

        {error && (
          <p role="alert" className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
            Could not load runs: {error}
          </p>
        )}

        {runs && runs.length === 0 ? (
          <p className="rounded-xl border border-dashed border-slate-300 px-4 py-10 text-center text-sm text-slate-500">
            No runs yet. Start a crawl on the onboarding page, or send a pull request webhook.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <ul className="min-w-0 space-y-2" aria-label="Recent runs">
              {(runs ?? []).map((run) => (
                <li key={run.run_id}>
                  <button
                    type="button"
                    onClick={() => setSelected(run.run_id)}
                    aria-current={selected === run.run_id ? "true" : undefined}
                    className={`w-full rounded-xl px-4 py-3 text-left ring-1 transition-colors ${
                      selected === run.run_id
                        ? "bg-white shadow-sm ring-brand-300"
                        : "bg-white/60 ring-slate-200 hover:bg-white"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                        {run.kind}
                      </span>
                      <RunStateBadge state={run.state} />
                    </div>
                    <p className="mt-1 truncate text-sm font-medium text-slate-900">{run.title}</p>
                    <p className="mt-0.5 truncate text-xs text-slate-500">
                      {when(run.started_at)}
                      {run.outcome ? ` · ${run.outcome}` : ""}
                    </p>
                  </button>
                </li>
              ))}
            </ul>
            <section
              aria-label="Run log"
              className="min-w-0 rounded-2xl bg-white p-6 shadow-sm ring-1 ring-slate-200 sm:p-8"
            >
              {selected ? (
                <RunLog key={selected} runId={selected} />
              ) : (
                <p className="text-sm text-slate-500">Select a run to see its log.</p>
              )}
            </section>
          </div>
        )}
      </main>
    </>
  );
}

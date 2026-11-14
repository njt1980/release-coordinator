import { useEffect, useState } from "react";
import Nav from "../components/Nav.jsx";
import { ROUTES } from "../lib/router.js";

const STORAGE_KEY = "rc-review-scope"; // shares the project scope with the Review and Inventory pages

// Gap kinds (core.models.ObservabilityGapKind): a title and what it costs the assessment.
const GAP_KINDS = {
  service_without_telemetry: {
    title: "Services telemetry never saw",
    effect: "Their calls can't be confirmed or refuted at runtime; only code and manifests speak for them.",
  },
  role_not_in_inventory: {
    title: "Telemetry roles that match no service",
    effect: "Calls they make or receive aren't attributed to a service: a rename, another application, or a name that differs from the Kubernetes one.",
  },
  no_role_name: {
    title: "Rows without a service name",
    effect: "Their calls can't be attributed to any service.",
  },
  no_namespace: {
    title: "Services matched by bare name",
    effect: "Their rows carry no Kubernetes namespace, so two services with the same name in different namespaces can't be told apart.",
  },
  no_request_telemetry: {
    title: "Services that write no requests",
    effect: "Calls to them can be confirmed only by address, not by joining the caller's call to the callee's request.",
  },
  sampled: {
    title: "Sampled services",
    effect: "Rare calls may be missing from telemetry, so a missing call is weaker evidence.",
  },
  consumer_not_traced: {
    title: "Messages whose consumers aren't traced",
    effect: "Who consumes what these services send isn't observed.",
  },
};

const STACKS = {
  appinsights_classic: "App Insights SDK 2.x",
  azure_monitor_otel: "Azure Monitor OpenTelemetry",
  otel_collector: "OpenTelemetry Collector",
  unknown: "Unknown",
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

const AUTO_PROBE_KEY = "rc-observability-probe";

/** Opens this page for a project (from a briefing link): remembers the scope and probes on arrival. */
export function openObservability(scope) {
  saveScope(scope);
  try {
    sessionStorage.setItem(AUTO_PROBE_KEY, "1");
  } catch {
    // Storage unavailable: the page opens without probing.
  }
}

function takeAutoProbe() {
  try {
    const flagged = sessionStorage.getItem(AUTO_PROBE_KEY) === "1";
    sessionStorage.removeItem(AUTO_PROBE_KEY);
    return flagged;
  } catch {
    return false;
  }
}

function seen(value) {
  if (!value) return "never";
  return `${value.slice(0, 10)} ${value.slice(11, 16)} UTC`;
}

/** The subject of a gap as a person reads it: the service name of an FQURI, else the role. */
function subjectLabel(subject) {
  if (!subject) return "(no service name)";
  return subject.includes(":") ? subject.split(":").pop() : subject;
}

function StackChips({ stacks }) {
  if (!stacks?.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {stacks.map((stack) => (
        <span key={stack} className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-700">
          {STACKS[stack] ?? stack}
        </span>
      ))}
    </span>
  );
}

function GapGroup({ kind, gaps }) {
  const meta = GAP_KINDS[kind] ?? { title: kind, effect: "" };
  return (
    <section className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
      <h3 className="text-sm font-semibold text-slate-900">
        {meta.title} <span className="font-normal text-slate-500">({gaps.length})</span>
      </h3>
      {meta.effect && <p className="mt-1 text-xs leading-relaxed text-slate-600">{meta.effect}</p>}
      <ul className="mt-3 divide-y divide-slate-100">
        {gaps.map((gap, index) => (
          <li key={`${gap.subject}-${index}`} className="flex flex-col gap-1 py-2 sm:flex-row sm:items-start sm:gap-3">
            <span className="shrink-0 font-medium text-slate-900 [overflow-wrap:anywhere] sm:w-56" title={gap.subject}>
              {subjectLabel(gap.subject)}
            </span>
            <span className="min-w-0 flex-1 text-sm text-slate-700">{gap.detail}</span>
            <StackChips stacks={gap.stacks} />
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Where the project's telemetry falls short, probed live from its workspace(s). */
export default function Observability() {
  const [scope, setScope] = useState(loadSaved);
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const url = `/api/v1/projects/${encodeURIComponent(scope.tenant)}/${encodeURIComponent(scope.projectId)}/observability`;

  const probe = async () => {
    setError(null);
    setLoading(true);
    saveScope(scope);
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

  useEffect(() => {
    if (takeAutoProbe()) probe();
    // Once, on arrival from a briefing link.
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

  const groups = {};
  for (const gap of report?.gaps ?? []) (groups[gap.kind] ??= []).push(gap);
  const kinds = [
    ...Object.keys(GAP_KINDS).filter((kind) => groups[kind]),
    ...Object.keys(groups).filter((kind) => !(kind in GAP_KINDS)),
  ];

  return (
    <>
      <Nav path={ROUTES.observability} tone="light" />
      <main className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
        <header className="mb-8 max-w-2xl">
          <p className="text-sm font-semibold uppercase tracking-wider text-brand-600">Telemetry</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">What telemetry can and can't confirm</h1>
          <p className="mt-3 text-sm leading-relaxed text-slate-600">
            Probes the telemetry workspace the newest baseline was built from (and the policy's additional workspaces)
            and lists, per service, where the runtime evidence falls short: services it never saw, names it can't
            match, sampling, and calls it can't attribute. Fixing these makes assessments stronger.
          </p>
        </header>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            probe();
          }}
          className="mb-6 flex flex-col gap-3 rounded-xl bg-white p-4 ring-1 ring-slate-200 sm:flex-row sm:items-end"
        >
          {field("tenant", "Tenant")}
          {field("projectId", "Project")}
          <button
            type="submit"
            disabled={loading}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-60"
          >
            {loading ? "Probing…" : "Probe telemetry"}
          </button>
          <p className="text-xs text-slate-500 sm:ml-auto sm:max-w-xs sm:self-center">
            Runs read-only queries against the workspace; it can take a few seconds.
          </p>
        </form>

        {error && (
          <p role="alert" className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}

        {report && (
          <>
            <dl className="mb-4 grid gap-x-6 gap-y-1 text-sm text-slate-600 sm:grid-cols-[max-content_1fr]">
              <dt className="font-medium text-slate-900">Baseline</dt>
              <dd className="font-mono [overflow-wrap:anywhere]">{report.snapshot_id}</dd>
              <dt className="font-medium text-slate-900">Workspace</dt>
              <dd className="font-mono [overflow-wrap:anywhere]">
                {[report.workspace_id, ...report.additional_workspaces].join(", ")}
              </dd>
              <dt className="font-medium text-slate-900">Scope</dt>
              <dd>{report.scope || "whole workspace"}, last {report.lookback_days} days</dd>
              <dt className="font-medium text-slate-900">Instrumentation</dt>
              <dd>
                {Object.keys(report.stacks).length
                  ? Object.entries(report.stacks)
                      .map(([stack, count]) => `${STACKS[stack] ?? stack}: ${count} role${count === 1 ? "" : "s"}`)
                      .join(" · ")
                  : "none seen"}
              </dd>
            </dl>
            <p className="mb-6 text-xs text-slate-500">
              <span className="font-semibold">Computed</span> at {seen(report.generated_at)} from telemetry rows; nothing
              is stored.
            </p>

            <h2 className="mb-3 text-lg font-semibold text-slate-900">
              {report.gaps.length ? `${report.gaps.length} gap${report.gaps.length === 1 ? "" : "s"}` : "No gaps found"}
            </h2>
            <div className="mb-10 grid gap-4">
              {kinds.map((kind) => (
                <GapGroup key={kind} kind={kind} gaps={groups[kind]} />
              ))}
            </div>

            <h2 className="mb-3 text-lg font-semibold text-slate-900">Telemetry roles ({report.roles.length})</h2>
            <div className="overflow-x-auto rounded-xl bg-white ring-1 ring-slate-200">
              <table className="min-w-full text-left text-sm">
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-3 py-2">Role</th>
                    <th scope="col" className="px-3 py-2">Instrumentation</th>
                    <th scope="col" className="px-3 py-2 text-right">Requests</th>
                    <th scope="col" className="px-3 py-2 text-right">Dependencies</th>
                    <th scope="col" className="px-3 py-2 text-right" title="Largest ItemCount on a row; above 1 means sampled">Sampling</th>
                    <th scope="col" className="px-3 py-2">Last seen</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {report.roles.map((role) => (
                    <tr key={`${role.namespace}/${role.role}/${role.components.join(",")}`} className="align-top">
                      <td className="px-3 py-2">
                        <span className="font-medium text-slate-900">{role.role || "(no service name)"}</span>
                        <span className="block text-xs text-slate-500">
                          {role.namespace || "no namespace"}
                          {role.components.length ? ` · ${role.components.join(", ")}` : ""}
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        <StackChips stacks={role.stacks} />
                      </td>
                      <td className={`px-3 py-2 text-right ${role.request_rows ? "text-slate-700" : "text-amber-800"}`}>
                        {role.request_rows}
                      </td>
                      <td className="px-3 py-2 text-right text-slate-700">{role.dependency_rows}</td>
                      <td className={`px-3 py-2 text-right ${role.max_item_count > 1 ? "text-amber-800" : "text-slate-400"}`}>
                        {role.max_item_count > 1 ? `1 in ${role.max_item_count}` : "none"}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-slate-700">{seen(role.last_seen)}</td>
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

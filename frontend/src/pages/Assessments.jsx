import { useEffect, useState } from "react";
import DecisionPanel from "../components/DecisionPanel.jsx";
import EgoGraph from "../components/EgoGraph.jsx";
import IdentityAnswer from "../components/IdentityAnswer.jsx";
import Nav from "../components/Nav.jsx";
import ReleaseTrain from "../components/ReleaseTrain.jsx";
import { ROUTES, href } from "../lib/router.js";
import { openObservability } from "./Observability.jsx";

const REFRESH_MS = 3000;
const API = "/api/v1/webhooks";

// How each rank reads. Labels sit next to colour so meaning never relies on it.
const RANKS = {
  decision: {
    title: "Decisions needed",
    blurb: "A person must decide or accept these.",
    badge: "Decision",
    className: "bg-amber-50 text-amber-800 ring-amber-200",
  },
  fix_evidence: {
    title: "Evidence to fix",
    blurb: "A declaration or configuration fix removes these.",
    badge: "Evidence fix",
    className: "bg-sky-50 text-sky-800 ring-sky-200",
  },
  info: {
    title: "Context",
    blurb: "Doesn't block; shown for the record.",
    badge: "Context",
    className: "bg-slate-100 text-slate-600 ring-slate-200",
  },
};

function Headline({ briefing }) {
  const go = briefing.approved;
  return (
    <div
      className={`rounded-xl px-4 py-3 ring-1 ${
        go ? "bg-emerald-50 ring-emerald-200" : "bg-red-50 ring-red-200"
      }`}
    >
      <p className={`text-sm font-semibold ${go ? "text-emerald-800" : "text-red-800"}`}>
        {go ? "✓ " : "✕ "}
        {briefing.headline}
      </p>
      <p className="mt-1 text-xs text-slate-600">
        {briefing.raw_issue_count} raw issue{briefing.raw_issue_count === 1 ? "" : "s"} grouped
        into {briefing.groups.filter((g) => g.issues.length).length} cause(s). The platform
        advises; the decision is yours.
      </p>
    </div>
  );
}

// Where each part of the briefing comes from (docs/intended-use.md).
const ORIGINS = {
  computed: { label: "Computed", className: "bg-white text-slate-600 ring-slate-300" },
  human: { label: "Human", className: "bg-white text-slate-800 ring-slate-400" },
  ai_generated: { label: "AI-generated", className: "bg-violet-50 text-violet-800 ring-violet-200" },
};

function OriginBadge({ origin }) {
  const meta = ORIGINS[origin] ?? ORIGINS.computed;
  return (
    <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${meta.className}`}>
      {meta.label}
    </span>
  );
}

function Advisory({ advisory }) {
  return (
    <section aria-label="Advisory risk review" className="rounded-xl bg-violet-50/40 p-4 ring-1 ring-violet-200">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-semibold text-slate-900">Advisory risk review</h4>
        <OriginBadge origin={advisory.origin} />
      </div>
      <p className="mt-1 text-xs text-slate-600">
        Risk {advisory.risk_score.toFixed(2)}; the model would {advisory.approved ? "approve" : "reject"} this release.
      </p>
      <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-slate-800 [overflow-wrap:anywhere]">
        {advisory.justification}
      </p>
      <p className="mt-2 text-xs text-slate-500">
        Written by an external model; it may be wrong and is not part of your signed decision. If you rely on it,
        say so in your decision's reason.
      </p>
    </section>
  );
}

function Group({ group, number, scope }) {
  const rank = RANKS[group.rank] ?? RANKS.info;
  return (
    <li className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h4 className="min-w-0 flex-1 text-sm font-semibold text-slate-900 [overflow-wrap:anywhere]">
          {number}. {group.title}
        </h4>
        <div className="flex shrink-0 gap-1">
          <OriginBadge origin={group.origin} />
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${rank.className}`}>
            {rank.badge}
          </span>
        </div>
      </div>
      {group.reason_codes.length > 0 && (
        <p className="mt-1 font-mono text-xs text-slate-500">{group.reason_codes.join(", ")}</p>
      )}
      {group.why_it_matters && (
        <p className="mt-2 text-sm leading-relaxed text-slate-600">{group.why_it_matters}</p>
      )}
      {group.services.length > 0 && (
        <p className="mt-2 text-xs text-slate-500 [overflow-wrap:anywhere]">
          Affects: {group.services.join(", ")}
        </p>
      )}
      {group.what_to_check.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Check</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-800">
            {group.what_to_check.map((item, i) => (
              <li key={i} className="[overflow-wrap:anywhere]">{item}</li>
            ))}
          </ul>
        </div>
      )}
      {group.how_to_close.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">How to close</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-800">
            {group.how_to_close.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </div>
      )}
      {group.root_key.startsWith("identity:") && scope && (
        <IdentityAnswer scope={scope} subjectKey={group.root_key} />
      )}
      {group.root_key === "info:observability" && scope && (
        <p className="mt-3 text-sm">
          <a
            href={href(ROUTES.observability)}
            onClick={() => openObservability(scope)}
            className="font-medium text-brand-600 hover:text-brand-700"
          >
            See the whole project's telemetry gaps →
          </a>
        </p>
      )}
      {group.policy_draft && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-medium text-brand-600 hover:text-brand-700">
            Draft policy change (for review; never applied)
          </summary>
          <pre className="mt-2 overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100">
            {group.policy_draft}
          </pre>
        </details>
      )}
      {group.issues.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs font-medium text-brand-600 hover:text-brand-700">
            {group.issues.length} raw line{group.issues.length === 1 ? "" : "s"}
          </summary>
          <ul className="mt-2 space-y-1 text-xs text-slate-600">
            {group.issues.map((issue, i) => (
              <li key={i} className="[overflow-wrap:anywhere]">
                <span className="font-mono">{issue.code}</span>: {issue.detail}
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}

function Briefing({ jobId, scope }) {
  const [briefing, setBriefing] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setBriefing(null);
    setError(null);
    fetch(`${API}/pr-assessment/${encodeURIComponent(jobId)}/briefing`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail ?? `Request failed with status ${response.status}`);
        }
        return response.json();
      })
      .then((data) => !cancelled && setBriefing(data))
      .catch((caught) => !cancelled && setError(caught.message));
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  if (error) return <p className="text-sm text-slate-500">{error}</p>;
  if (!briefing) return <p className="text-sm text-slate-500">Loading the briefing…</p>;
  let number = 0;
  return (
    <div className="space-y-6">
      <div>
        <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">Release briefing</p>
        <h3 className="mt-1 text-xl font-semibold text-slate-900">{briefing.pr_id}</h3>
        {briefing.head_sha && (
          <p className="mt-0.5 font-mono text-xs text-slate-500">head {briefing.head_sha.slice(0, 12)}</p>
        )}
      </div>
      <Headline briefing={briefing} />
      {briefing.advisory && <Advisory advisory={briefing.advisory} />}
      <DecisionPanel jobId={jobId} toolApproved={briefing.approved} />
      {Object.entries(RANKS).map(([rank, meta]) => {
        const groups = briefing.groups.filter((g) => g.rank === rank);
        if (!groups.length) return null;
        return (
          <section key={rank} aria-label={meta.title}>
            <h4 className="text-sm font-semibold text-slate-900">{meta.title}</h4>
            <p className="text-xs text-slate-500">{meta.blurb}</p>
            <ol className="mt-3 space-y-3">
              {groups.map((group) => {
                number += 1;
                return <Group key={group.root_key} group={group} number={number} scope={scope} />;
              })}
            </ol>
          </section>
        );
      })}
      {briefing.test_scope?.length > 0 && (
        <section aria-label="Suggested test scope">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="text-sm font-semibold text-slate-900">Suggested test scope</h4>
            <OriginBadge origin="computed" />
          </div>
          <p className="text-xs text-slate-500">
            By distance from the change in the dependency graph; only as complete as the graph.
          </p>
          <ol className="mt-2 space-y-2">
            {briefing.test_scope.map((tier) => (
              <li key={tier.tier} className="rounded-xl bg-white px-4 py-3 ring-1 ring-slate-200">
                <p className="text-sm font-medium text-slate-900">
                  {tier.tier}. {tier.title}
                </p>
                <p className="mt-1 text-sm text-slate-800 [overflow-wrap:anywhere]">{tier.services.join(", ")}</p>
                <p className="mt-1 text-xs text-slate-500">{tier.why}</p>
              </li>
            ))}
          </ol>
        </section>
      )}
      <section aria-label="Dependency map">
        <div className="flex flex-wrap items-center gap-2">
          <h4 className="text-sm font-semibold text-slate-900">Dependency map</h4>
          <OriginBadge origin="computed" />
        </div>
        <p className="mb-2 text-xs text-slate-500">
          The changed services and two hops each way, in the baseline this assessment used.
        </p>
        <EgoGraph url={`${API}/pr-assessment/${encodeURIComponent(jobId)}/graph?depth=2`} centerLabel="Changed" />
      </section>
      {briefing.deployment.length > 0 && (
        <section aria-label="Deployment order">
          <h4 className="text-sm font-semibold text-slate-900">Deployment order</h4>
          <ol className="mt-2 flex flex-wrap gap-2 text-sm">
            {briefing.deployment.map((step) => (
              <li
                key={step.order}
                title={step.note || undefined}
                className={`rounded-full px-3 py-1 ring-1 ${
                  step.changed ? "bg-brand-50 text-brand-800 ring-brand-200" : "bg-white text-slate-600 ring-slate-200"
                }`}
              >
                {step.order}. {step.service}
                {step.changed ? " (changed)" : ""}
              </li>
            ))}
          </ol>
          {briefing.rollback_order.length > 0 && (
            <p className="mt-2 text-xs text-slate-500">Rollback: {briefing.rollback_order.join(" → ")}</p>
          )}
        </section>
      )}
    </div>
  );
}

// Contract change kinds the coordinator chooses (they set the deployment order).
const CHANGE_TYPES = [
  { value: "additive", label: "Additive (compatible)" },
  { value: "breaking", label: "Breaking (tightening)" },
];

function detailText(detail) {
  if (!detail) return null;
  if (typeof detail === "string") return detail;
  if (detail.message) return detail.message;
  return JSON.stringify(detail);
}

/** Assess a pull request named by its Azure DevOps ID (read-only intake). */
function AssessPrForm({ onScheduled }) {
  const [prId, setPrId] = useState("");
  const [changeType, setChangeType] = useState("additive");
  const [tenant, setTenant] = useState("");
  const [projectId, setProjectId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const number = Number.parseInt(prId, 10);
  const valid = /^\d+$/.test(prId.trim()) && number > 0;

  const submit = async (event) => {
    event.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    const body = { pr_id: number, change_type: changeType };
    if (tenant.trim()) body.tenant = tenant.trim();
    if (projectId.trim()) body.project_id = projectId.trim();
    try {
      const response = await fetch(`${API}/pr-intake`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(detailText(data.detail) ?? `Request failed with status ${response.status}`);
      const skipped = data.skipped_files?.length
        ? ` ${data.skipped_files.length} changed file(s) are not in the diff the risk review reads.`
        : "";
      setNotice(`Assessing PR ${data.pr_id} in ${data.repository}: ${data.title || "untitled"}.${skipped}`);
      setPrId("");
      onScheduled(data.job_id);
    } catch (caught) {
      setError(caught.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      onSubmit={submit}
      aria-label="Assess a pull request"
      className="mb-8 rounded-2xl bg-white p-4 shadow-sm ring-1 ring-slate-200 sm:p-5"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <label className="block sm:w-40">
          <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Pull request #</span>
          <input
            inputMode="numeric"
            value={prId}
            onChange={(e) => setPrId(e.target.value)}
            placeholder="1234"
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
          />
        </label>
        <label className="block sm:w-56">
          <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Contract change</span>
          <select
            value={changeType}
            onChange={(e) => setChangeType(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
          >
            {CHANGE_TYPES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          disabled={!valid || busy}
          className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Reading the PR…" : valid ? `Assess PR #${number}` : "Assess PR"}
        </button>
      </div>
      <details className="mt-3">
        <summary className="cursor-pointer text-xs font-medium text-brand-600 hover:text-brand-700">
          Project scope (defaults to the server's)
        </summary>
        <div className="mt-2 flex flex-col gap-3 sm:flex-row">
          <input
            aria-label="Tenant"
            value={tenant}
            onChange={(e) => setTenant(e.target.value)}
            placeholder="tenant"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm sm:w-48"
          />
          <input
            aria-label="Project"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            placeholder="project"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm sm:w-48"
          />
        </div>
      </details>
      <p className="mt-3 text-xs text-slate-500">
        Read-only: the PR's latest iteration is read from Azure DevOps with the project's read identity.
        Nothing is posted back.
      </p>
      {error && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 [overflow-wrap:anywhere]">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="mt-3 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-800 [overflow-wrap:anywhere]">
          {notice}
        </p>
      )}
    </form>
  );
}

/** Pull request assessments, each as a release briefing for the coordinator. */
export default function Assessments() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    const load = async () => {
      try {
        const response = await fetch(`${API}/pr-assessments`);
        if (!response.ok) throw new Error(`Request failed with status ${response.status}`);
        const data = await response.json();
        if (cancelled) return;
        setItems(data);
        setError(null);
        setSelected((current) => current ?? data.find((d) => d.status === "completed")?.job_id ?? null);
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
      <Nav path={ROUTES.assessments} tone="light" />
      <main className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
        <header className="mb-8 max-w-2xl">
          <p className="text-sm font-semibold uppercase tracking-wider text-brand-600">Assessments</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">
            What each release needs from you
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-slate-600">
            Each pull request's evidence, grouped by cause and ranked: decisions first, then
            evidence to fix, then context. Nothing is hidden; the raw lines stay under each group.
          </p>
        </header>

        <AssessPrForm onScheduled={setSelected} />

        {error && (
          <p role="alert" className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
            Could not load assessments: {error}
          </p>
        )}

        {items && items.length === 0 ? (
          <p className="rounded-xl border border-dashed border-slate-300 px-4 py-10 text-center text-sm text-slate-500">
            No assessments yet in this session. Enter a pull request number above to start one.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <ul className="min-w-0 space-y-2" aria-label="Assessments">
              {(items ?? []).map((item) => (
                <li key={item.job_id}>
                  <button
                    type="button"
                    onClick={() => setSelected(item.job_id)}
                    aria-current={selected === item.job_id ? "true" : undefined}
                    className={`w-full rounded-xl px-4 py-3 text-left ring-1 transition-colors ${
                      selected === item.job_id
                        ? "bg-white shadow-sm ring-brand-300"
                        : "bg-white/60 ring-slate-200 hover:bg-white"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium text-slate-900">{item.pr_id}</span>
                      <span className="shrink-0 text-xs uppercase tracking-wide text-slate-500">{item.status}</span>
                    </div>
                    {item.headline && (
                      <p className="mt-1 line-clamp-2 text-xs text-slate-600">{item.headline}</p>
                    )}
                  </button>
                </li>
              ))}
            </ul>
            <section
              aria-label="Release briefing"
              className="min-w-0 rounded-2xl bg-slate-50 p-6 shadow-sm ring-1 ring-slate-200 sm:p-8"
            >
              {selected ? (
                <Briefing
                  scope={(() => {
                    const item = (items ?? []).find((i) => i.job_id === selected);
                    return item?.tenant ? { tenant: item.tenant, projectId: item.project_id } : null;
                  })()}
                  key={`${selected}:${(items ?? []).find((i) => i.job_id === selected)?.status ?? ""}`}
                  jobId={selected}
                />
              ) : (
                <p className="text-sm text-slate-500">Select a completed assessment to see its briefing.</p>
              )}
            </section>
          </div>
        )}

        <ReleaseTrain items={items} />
      </main>
    </>
  );
}

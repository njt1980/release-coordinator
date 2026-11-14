import { useCallback, useEffect, useMemo, useState } from "react";
import Nav from "../components/Nav.jsx";
import { ROUTES } from "../lib/router.js";

const STORAGE_KEY = "rc-review-scope";

// Labels sit next to colour so meaning never relies on it.
const STATUS = {
  open: { label: "Open", className: "bg-amber-50 text-amber-800 ring-amber-200" },
  needs_rereview: { label: "Needs re-review", className: "bg-red-50 text-red-700 ring-red-200" },
  confirmed: { label: "Confirmed", className: "bg-emerald-50 text-emerald-800 ring-emerald-200" },
  added: { label: "Added", className: "bg-brand-50 text-brand-800 ring-brand-200" },
  removed: { label: "Removed", className: "bg-slate-100 text-slate-600 ring-slate-200" },
  answered: { label: "Answered", className: "bg-emerald-50 text-emerald-800 ring-emerald-200" },
  linked: { label: "Linked", className: "bg-emerald-50 text-emerald-800 ring-emerald-200" },
};

const short = (id) => (id ?? "").split(":").pop();
const rowId = (key) => `review-item-${key}`;

// Queue levels, weakest evidence first (see src/orchestration/review_queue.py).
const LEVELS = {
  1: { label: "Re-review", className: "bg-red-50 text-red-700 ring-red-200" },
  2: { label: "Question", className: "bg-amber-50 text-amber-800 ring-amber-200" },
  3: { label: "Weak evidence", className: "bg-amber-50 text-amber-800 ring-amber-200" },
  4: { label: "One source", className: "bg-sky-50 text-sky-800 ring-sky-200" },
  5: { label: "Corroborated", className: "bg-slate-100 text-slate-600 ring-slate-200" },
};
const QUEUE_SHOWN = 10;

function Queue({ queue, onOpen }) {
  const [all, setAll] = useState(false);
  if (!queue?.length) return null;
  const shown = all ? queue : queue.slice(0, QUEUE_SHOWN);
  return (
    <section aria-labelledby="queue-heading">
      <h2 id="queue-heading" className="text-lg font-semibold text-slate-900">Next to review</h2>
      <p className="text-sm text-slate-500">
        {queue.length} unanswered item(s), weakest evidence first; ties go to the more critical service.
      </p>
      <ol className="mt-3 divide-y divide-slate-100 rounded-xl bg-white ring-1 ring-slate-200">
        {shown.map((item) => {
          const level = LEVELS[item.level] ?? LEVELS[5];
          return (
            <li key={item.key} className="flex flex-col gap-1 px-4 py-3 sm:flex-row sm:items-center sm:gap-3">
              <span className="w-6 shrink-0 text-sm text-slate-400">{item.position}</span>
              <span className={`w-fit shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${level.className}`}>
                {level.label}
              </span>
              <div className="min-w-0 flex-1">
                <button
                  type="button"
                  onClick={() => onOpen(item)}
                  className="text-left text-sm font-medium text-brand-700 hover:underline [overflow-wrap:anywhere]"
                >
                  {item.title}
                </button>
                <p className="text-xs text-slate-500 [overflow-wrap:anywhere]">{item.why}</p>
              </div>
              <span className="shrink-0 text-xs text-slate-600">{item.owners.length ? item.owners.join(", ") : "no owner recorded"}</span>
            </li>
          );
        })}
      </ol>
      {queue.length > QUEUE_SHOWN && (
        <button type="button" onClick={() => setAll(!all)} className="mt-2 text-xs font-medium text-brand-600 hover:text-brand-700">
          {all ? "Show the first ten" : `Show all ${queue.length}`}
        </button>
      )}
    </section>
  );
}

function loadSaved() {
  try {
    return { tenant: "default", projectId: "default", reviewer: "", ...JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") };
  } catch {
    return { tenant: "default", projectId: "default", reviewer: "" };
  }
}

function Status({ status }) {
  const meta = STATUS[status] ?? STATUS.open;
  return <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${meta.className}`}>{meta.label}</span>;
}

function AnswerLine({ item }) {
  const answer = item.answer ?? item.previous;
  if (!answer) return null;
  const when = new Date(answer.answered_at).toLocaleDateString();
  return (
    <div className="mt-2 text-xs text-slate-600">
      <p>
        {item.answer ? "" : "Previously: "}
        <span className="font-medium">{answer.verdict.replace("_", " ")}</span> by {answer.reviewer} on {when}
        {answer.reason ? `: ${answer.reason}` : ""}
      </p>
      {item.stale.map((reason, i) => (
        <p key={i} className="text-red-700 [overflow-wrap:anywhere]">
          No longer counts: {reason.detail}
          {reason.changed_paths.length ? ` (${reason.changed_paths.join(", ")})` : ""}
        </p>
      ))}
    </div>
  );
}

/** Buttons that open a small form; ``needsReason`` asks "how do you know". */
function Actions({ actions, disabled, onSubmit }) {
  const [open, setOpen] = useState(null);
  const [reason, setReason] = useState("");
  const [extra, setExtra] = useState("");
  const action = actions.find((a) => a.verdict === open);
  const submit = (event) => {
    event.preventDefault();
    onSubmit(action, reason.trim(), extra).then((ok) => ok && (setOpen(null), setReason(""), setExtra("")));
  };
  if (disabled) return <p className="mt-3 text-xs text-slate-500">{disabled}</p>;
  return (
    <div className="mt-3">
      <div className="flex flex-wrap gap-2">
        {actions.map((a) => (
          <button
            key={a.verdict}
            type="button"
            aria-expanded={open === a.verdict}
            onClick={() => (a.needsReason || a.extra ? setOpen(open === a.verdict ? null : a.verdict) : onSubmit(a, "", ""))}
            className="rounded-md bg-white px-3 py-1.5 text-xs font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50"
          >
            {a.label}
          </button>
        ))}
      </div>
      {action && (
        <form onSubmit={submit} className="mt-3 space-y-2">
          {action.extra && (
            <label className="block text-xs font-medium text-slate-600">
              {action.extra.label}
              {action.extra.options ? (
                <select
                  required
                  value={extra}
                  onChange={(e) => setExtra(e.target.value)}
                  className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                >
                  <option value="">Choose…</option>
                  {action.extra.options.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              ) : (
                <input required value={extra} onChange={(e) => setExtra(e.target.value)} className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm" />
              )}
            </label>
          )}
          {action.needsReason && (
            <label className="block text-xs font-medium text-slate-600">
              {action.reasonLabel ?? "Reason"}
              <textarea required rows={2} value={reason} onChange={(e) => setReason(e.target.value)} className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm" />
            </label>
          )}
          <button type="submit" className="rounded-md bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-700">
            Record: {action.label}
          </button>
        </form>
      )}
    </div>
  );
}

function Question({ question, services, answer, reviewer }) {
  const identity = question.subject.kind === "identity";
  const callers = question.services;
  const actions = identity
    ? [{ verdict: "link", label: "Link to a running service", extra: { label: "Runtime service name" } }]
    : [
        { verdict: "nothing_hidden", label: "Nothing hidden", needsReason: true, reasonLabel: "How do you know nothing is behind this?" },
        {
          verdict: "calls",
          label: "It calls…",
          needsReason: true,
          reasonLabel: "How do you know?",
          extra: { label: `${callers.length ? short(callers[0]) : "The caller"} calls`, options: services.map((s) => ({ value: s.service_id, label: s.name })) },
        },
      ];
  const onSubmit = (action, reason, extra) => {
    const body = { subject: question.subject, verdict: action.verdict, reason: reason || null };
    if (action.verdict === "link") body.linked_runtime_name = extra.trim();
    if (action.verdict === "calls") {
      if (!callers.length) return Promise.resolve(false);
      body.declared = [{ source_id: callers[0], target_id: extra }];
    }
    return answer(body);
  };
  return (
    <li id={rowId(question.subject.key)} className="scroll-mt-24 rounded-xl bg-white p-4 ring-1 ring-slate-200">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="min-w-0 flex-1 font-mono text-sm font-semibold text-slate-900 [overflow-wrap:anywhere]">{question.subject.key}</h3>
        <Status status={question.status} />
      </div>
      <p className="mt-1 font-mono text-xs text-slate-500">{question.code}</p>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-700">
        {question.details.slice(0, 3).map((d, i) => (
          <li key={i} className="[overflow-wrap:anywhere]">{d}</li>
        ))}
      </ul>
      {question.details.length > 3 && <p className="mt-1 text-xs text-slate-500">…and {question.details.length - 3} more</p>}
      {callers.length > 0 && <p className="mt-2 text-xs text-slate-500">Services: {callers.map(short).join(", ")}</p>}
      <AnswerLine item={question} />
      {question.status !== "answered" && question.status !== "linked" && (
        <Actions
          actions={actions}
          disabled={!reviewer ? "Enter your name above to answer." : !identity && !question.anchors ? "Rests on nothing in a scanned repository; can't be answered here." : null}
          onSubmit={onSubmit}
        />
      )}
    </li>
  );
}

function Dependency({ row, answer, reviewer }) {
  const onSubmit = (action, reason) => answer({ subject: row.subject, verdict: action.verdict, reason: reason || null });
  const settled = ["confirmed", "removed", "added"].includes(row.status);
  return (
    <li id={rowId(row.subject.key)} className="scroll-mt-24 rounded-xl bg-white p-4 ring-1 ring-slate-200">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="min-w-0 flex-1 text-sm font-semibold text-slate-900 [overflow-wrap:anywhere]">
          {short(row.source_id)} → {short(row.target_id)}
          {row.protocol ? <span className="ml-2 font-mono text-xs font-normal text-slate-500">{row.protocol}</span> : null}
        </h3>
        <Status status={row.status} />
      </div>
      <p className="mt-1 text-xs text-slate-500">
        {row.discovery_methods.join(", ")} · {row.provenance}
      </p>
      {row.weak && <p className="mt-1 text-xs font-medium text-amber-700">Weak evidence: {row.weak}</p>}
      {row.evidence_files?.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs font-medium text-brand-600 hover:text-brand-700">
            {row.evidence_files.length} file{row.evidence_files.length === 1 ? "" : "s"} cite it
          </summary>
          <ul className="mt-1 space-y-0.5 font-mono text-xs text-slate-600">
            {row.evidence_files.map((f) => (
              <li key={f} className="[overflow-wrap:anywhere]">{f}</li>
            ))}
          </ul>
        </details>
      )}
      <AnswerLine item={row} />
      {!settled && (
        <Actions
          actions={[
            { verdict: "agree", label: "Agree" },
            { verdict: "disagree", label: "Disagree", needsReason: true, reasonLabel: "Why is this not a real dependency?" },
          ]}
          disabled={!reviewer ? "Enter your name above to answer." : !row.anchors ? "The caller has no scanned repository; can't be reviewed here." : null}
          onSubmit={onSubmit}
        />
      )}
    </li>
  );
}

function AddDependency({ services, answer, reviewer }) {
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const submit = (event) => {
    event.preventDefault();
    answer({
      subject: { kind: "dependency", key: `edge:${source}->${target}` },
      verdict: "add",
      reason: reason.trim(),
      declared: [{ source_id: source, target_id: target }],
    }).then((ok) => ok && (setSource(""), setTarget(""), setReason("")));
  };
  const select = (value, set, label) => (
    <label className="block min-w-0 flex-1 text-xs font-medium text-slate-600">
      {label}
      <select required value={value} onChange={(e) => set(e.target.value)} className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm">
        <option value="">Choose…</option>
        {services.map((s) => (
          <option key={s.service_id} value={s.service_id}>{s.name}</option>
        ))}
      </select>
    </label>
  );
  return (
    <form onSubmit={submit} className="space-y-3 rounded-xl bg-white p-4 ring-1 ring-slate-200">
      <div className="flex flex-col gap-3 sm:flex-row">
        {select(source, setSource, "Service")}
        {select(target, setTarget, "calls")}
      </div>
      <label className="block text-xs font-medium text-slate-600">
        How do you know?
        <textarea required rows={2} value={reason} onChange={(e) => setReason(e.target.value)} className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm" />
      </label>
      <button
        type="submit"
        disabled={!reviewer || !source || !target || source === target}
        className="rounded-md bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        Add dependency
      </button>
    </form>
  );
}

const FILTERS = {
  todo: { label: "To review", test: (r) => r.status === "open" || r.status === "needs_rereview" },
  weak: { label: "Weak evidence", test: (r) => Boolean(r.weak) },
  all: { label: "All", test: () => true },
};

/** Once per project: check the dependency map the platform found and correct it. */
export default function Review() {
  const [scope, setScope] = useState(loadSaved);
  const [review, setReview] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [filter, setFilter] = useState("todo");
  const base = `/api/v1/projects/${encodeURIComponent(scope.tenant)}/${encodeURIComponent(scope.projectId)}/review`;

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(scope));
    } catch {
      // Storage unavailable: the scope just isn't remembered.
    }
  }, [scope]);

  const load = useCallback(async () => {
    setError(null);
    try {
      const response = await fetch(base);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail ?? `Request failed with status ${response.status}`);
      setReview(body);
    } catch (caught) {
      setReview(null);
      setError(caught.message);
    }
  }, [base]);

  useEffect(() => {
    load();
  }, [load]);

  const answer = useCallback(
    async (body) => {
      setNotice(null);
      const response = await fetch(`${base}/answers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, reviewer: scope.reviewer }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        setNotice({ error: true, text: typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail) });
        return false;
      }
      setReview(data);
      setNotice({ error: false, text: "Answer recorded." });
      return true;
    },
    [base, scope.reviewer],
  );

  const counts = useMemo(() => {
    if (!review) return null;
    const all = [...review.dependencies, ...review.questions];
    return {
      questions: review.questions.filter((q) => q.status === "open").length,
      dependencies: review.dependencies.filter((d) => d.status === "open").length,
      stale: all.filter((i) => i.status === "needs_rereview").length,
    };
  }, [review]);

  const field = (key, label, props = {}) => (
    <label className="block min-w-0 flex-1 text-xs font-medium text-slate-600">
      {label}
      <input
        value={scope[key]}
        onChange={(e) => setScope({ ...scope, [key]: e.target.value })}
        className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        {...props}
      />
    </label>
  );

  const rows = review ? review.dependencies.filter(FILTERS[filter].test) : [];
  return (
    <>
      <Nav path={ROUTES.review} tone="light" />
      <main className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
        <header className="mb-8 max-w-2xl">
          <p className="text-sm font-semibold uppercase tracking-wider text-brand-600">Dependency review</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">Confirm the map every release relies on</h1>
          <p className="mt-3 text-sm leading-relaxed text-slate-600">
            Agree with or correct each dependency, add what the platform missed, and answer what it couldn't see.
            Every assessment then uses the confirmed map. An answer stops counting when the files it rests on change.
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
          {field("reviewer", "Your name", { placeholder: "Recorded with each answer" })}
          <button type="submit" className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700">
            Load
          </button>
        </form>

        {error && (
          <p role="alert" className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}
        {notice && (
          <p role="status" className={`mb-4 rounded-md px-3 py-2 text-sm ${notice.error ? "bg-red-50 text-red-700" : "bg-emerald-50 text-emerald-800"}`}>
            {notice.text}
          </p>
        )}

        {review && (
          <div className="space-y-10">
            <p className="text-sm text-slate-600">
              Baseline <span className="font-mono">{review.snapshot_id}</span>: {review.services.length} services,{" "}
              {counts.questions} open question(s), {counts.dependencies} unreviewed dependenc{counts.dependencies === 1 ? "y" : "ies"}
              {counts.stale ? `, ${counts.stale} needing re-review` : ""}.{" "}
              <a className="font-medium text-brand-600 hover:text-brand-700" href={`${base}/export`} target="_blank" rel="noreferrer">
                Export corrections
              </a>
            </p>
            {review.unapplied.length > 0 && (
              <ul className="rounded-md bg-amber-50 px-4 py-3 text-sm text-amber-800">
                {review.unapplied.map((u, i) => (
                  <li key={i} className="[overflow-wrap:anywhere]">Not applied: {u}</li>
                ))}
              </ul>
            )}

            <Queue
              queue={review.queue}
              onOpen={(item) => {
                if (item.kind === "dependency") setFilter("all");
                // Let the filter render the row, then bring it into view.
                setTimeout(() => {
                  const element = document.getElementById(rowId(item.key));
                  element?.scrollIntoView({ behavior: "smooth", block: "start" });
                  element?.focus?.();
                }, 0);
              }}
            />

            <section aria-labelledby="questions-heading">
              <h2 id="questions-heading" className="text-lg font-semibold text-slate-900">Questions</h2>
              <p className="text-sm text-slate-500">What the platform couldn't see, one entry per cause.</p>
              {review.questions.length === 0 ? (
                <p className="mt-3 text-sm text-slate-500">No open questions.</p>
              ) : (
                <ol className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
                  {review.questions.map((q) => (
                    <Question key={q.subject.key} question={q} services={review.services} answer={answer} reviewer={scope.reviewer.trim()} />
                  ))}
                </ol>
              )}
            </section>

            <section aria-labelledby="dependencies-heading">
              <div className="flex flex-wrap items-end justify-between gap-3">
                <div>
                  <h2 id="dependencies-heading" className="text-lg font-semibold text-slate-900">Dependencies</h2>
                  <p className="text-sm text-slate-500">Caller → callee, with the evidence behind each.</p>
                </div>
                <div className="flex gap-1" role="group" aria-label="Filter dependencies">
                  {Object.entries(FILTERS).map(([key, meta]) => (
                    <button
                      key={key}
                      type="button"
                      aria-pressed={filter === key}
                      onClick={() => setFilter(key)}
                      className={`rounded-md px-3 py-1.5 text-xs font-medium ring-1 ${
                        filter === key ? "bg-slate-900 text-white ring-slate-900" : "bg-white text-slate-700 ring-slate-300 hover:bg-slate-50"
                      }`}
                    >
                      {meta.label}
                    </button>
                  ))}
                </div>
              </div>
              {rows.length === 0 ? (
                <p className="mt-3 text-sm text-slate-500">Nothing here.</p>
              ) : (
                <ol className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
                  {rows.map((row) => (
                    <Dependency key={row.subject.key} row={row} answer={answer} reviewer={scope.reviewer.trim()} />
                  ))}
                </ol>
              )}
            </section>

            <section aria-labelledby="add-heading">
              <h2 id="add-heading" className="text-lg font-semibold text-slate-900">Add a missing dependency</h2>
              <p className="mb-3 text-sm text-slate-500">Declared dependencies count for ordering and are labelled as declared, never as automated evidence.</p>
              <AddDependency services={review.services} answer={answer} reviewer={scope.reviewer.trim()} />
            </section>
          </div>
        )}
      </main>
    </>
  );
}

import { useEffect, useState } from "react";

const API = "/api/v1";
// Shared with the Review page, so a reviewer types their name once.
const REVIEW_STORAGE_KEY = "rc-review-scope";

function storedReviewer() {
  try {
    return JSON.parse(localStorage.getItem(REVIEW_STORAGE_KEY) ?? "{}").reviewer ?? "";
  } catch {
    return "";
  }
}

function rememberReviewer(reviewer) {
  try {
    const saved = JSON.parse(localStorage.getItem(REVIEW_STORAGE_KEY) ?? "{}");
    localStorage.setItem(REVIEW_STORAGE_KEY, JSON.stringify({ ...saved, reviewer }));
  } catch {
    // Storage unavailable: the name is simply not remembered.
  }
}

async function readJson(response) {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : null;
    throw new Error(detail ?? `Request failed with status ${response.status}`);
  }
  return response.json();
}

/**
 * Answer a briefing's identity question ("source code not linked to a
 * running service") in place. The answer goes to the project's dependency
 * review, the same store and endpoint as the Review page, so it applies
 * from the next assessment of the project; this briefing stays as it was.
 */
export default function IdentityAnswer({ scope, subjectKey }) {
  const base = `${API}/projects/${encodeURIComponent(scope.tenant)}/${encodeURIComponent(scope.projectId)}/review`;
  const [question, setQuestion] = useState(undefined); // undefined: loading; null: not open
  const [services, setServices] = useState([]);
  const [signer, setSigner] = useState(null);
  const [reviewer, setReviewer] = useState(storedReviewer);
  const [runtimeName, setRuntimeName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const take = (review) => {
    setQuestion(review.questions.find((q) => q.subject.key === subjectKey) ?? null);
    setServices(review.services);
  };

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetch(base).then(readJson), fetch(`${API}/decisions/signer`).then(readJson).catch(() => null)])
      .then(([review, who]) => {
        if (cancelled) return;
        take(review);
        setSigner(who);
      })
      .catch((caught) => !cancelled && setError(caught.message));
    return () => {
      cancelled = true;
    };
  }, [base, subjectKey]);

  const signedIn = signer && signer.source !== "typed" && signer.name;
  const name = signedIn ? signer.name : reviewer.trim();

  const submit = async (event) => {
    event.preventDefault();
    if (!name || !runtimeName.trim()) return;
    setBusy(true);
    setError(null);
    try {
      if (!signedIn) rememberReviewer(reviewer.trim());
      const review = await fetch(`${base}/answers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          subject: question.subject,
          verdict: "link",
          reviewer: name,
          linked_runtime_name: runtimeName.trim(),
        }),
      }).then(readJson);
      take(review);
      setRuntimeName("");
    } catch (caught) {
      setError(caught.message);
    } finally {
      setBusy(false);
    }
  };

  if (error && question === undefined) {
    return <p className="mt-3 text-xs text-slate-500">The dependency review can't be read: {error}</p>;
  }
  if (question === undefined) return <p className="mt-3 text-xs text-slate-500">Reading the dependency review…</p>;
  if (question === null) {
    return (
      <p className="mt-3 text-xs text-slate-500">
        Not an open question in the project's newest baseline, so it can't be answered here.
      </p>
    );
  }
  const answer = question.answer;
  if (answer && question.status === "linked") {
    return (
      <p className="mt-3 text-sm text-emerald-800 [overflow-wrap:anywhere]">
        Linked to <span className="font-mono">{answer.linked_runtime_name}</span> by {answer.reviewer} on{" "}
        {new Date(answer.answered_at).toLocaleDateString()}. Applies from the next assessment of this project.
      </p>
    );
  }
  const candidates = services.filter((s) => !s.repository).map((s) => s.name);
  const listId = `runtime-names-${subjectKey}`;
  return (
    <form onSubmit={submit} className="mt-3 space-y-2 rounded-lg bg-slate-50 p-3 ring-1 ring-slate-200">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Answer</p>
      <label className="block text-xs font-medium text-slate-600">
        Deployed as (running service name)
        <input
          required
          list={listId}
          value={runtimeName}
          onChange={(e) => setRuntimeName(e.target.value)}
          className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        />
        <datalist id={listId}>
          {candidates.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
      </label>
      {signedIn ? (
        <p className="text-xs text-slate-600">Recorded as {signer.name}.</p>
      ) : (
        <label className="block text-xs font-medium text-slate-600">
          Your name
          <input
            required
            value={reviewer}
            onChange={(e) => setReviewer(e.target.value)}
            placeholder="Recorded with the answer"
            className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          />
        </label>
      )}
      {error && <p className="text-xs text-red-700 [overflow-wrap:anywhere]">{error}</p>}
      <button
        type="submit"
        disabled={busy || !name || !runtimeName.trim()}
        className="rounded-md bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-700 disabled:opacity-50"
      >
        Record: link to a running service
      </button>
      <p className="text-xs text-slate-500">
        Saved in the project's dependency review. It applies from the next assessment; this briefing stays as it was.
      </p>
    </form>
  );
}

import { useEffect, useRef, useState } from "react";

// What each event status looks like. Text labels sit next to the icons so
// the meaning never relies on colour alone.
const STATUS = {
  info: { icon: "•", label: "Info", className: "bg-slate-100 text-slate-600 ring-slate-200" },
  passed: { icon: "✓", label: "Passed", className: "bg-emerald-50 text-emerald-700 ring-emerald-200" },
  warning: { icon: "!", label: "Warning", className: "bg-amber-50 text-amber-800 ring-amber-200" },
  blocked: { icon: "✕", label: "Blocked", className: "bg-red-50 text-red-700 ring-red-200" },
  failed: { icon: "✕", label: "Failed", className: "bg-red-100 text-red-800 ring-red-300" },
};

const POLL_MS = 1000;

const time = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function EventRow({ event }) {
  const status = STATUS[event.status] ?? STATUS.info;
  return (
    <li className="relative flex gap-3 pb-4 last:pb-0">
      <span
        className={`relative z-10 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold ring-1 ${status.className}`}
        aria-hidden="true"
      >
        {status.icon}
      </span>
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-baseline gap-x-2 text-xs text-slate-500">
          <span className="font-mono">{time(event.at)}</span>
          <span className="font-semibold uppercase tracking-wide text-slate-700">{event.stage}</span>
          <span className="sr-only">{status.label}:</span>
        </p>
        <p className="mt-0.5 text-sm leading-relaxed text-slate-800 [overflow-wrap:anywhere]">
          {event.message}
        </p>
        {event.details?.length > 0 && (
          <details className="mt-1">
            <summary className="cursor-pointer text-xs font-medium text-brand-600 hover:text-brand-700">
              {event.details.length} detail{event.details.length === 1 ? "" : "s"}
            </summary>
            <ul className="mt-1 space-y-0.5 rounded-md bg-slate-50 px-3 py-2 font-mono text-xs text-slate-700 [overflow-wrap:anywhere]">
              {event.details.map((detail, index) => (
                <li key={index}>{detail}</li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </li>
  );
}

/** A static list of events (for example a log kept with a baseline). */
export function EventTimeline({ events, emptyText = "No steps recorded yet." }) {
  if (!events.length) {
    return <p className="text-sm text-slate-500">{emptyText}</p>;
  }
  return (
    <ol className="relative before:absolute before:bottom-3 before:left-3 before:top-3 before:w-px before:bg-slate-200">
      {events.map((event) => (
        <EventRow key={event.seq} event={event} />
      ))}
    </ol>
  );
}

const STATE_BADGE = {
  running: "bg-brand-50 text-brand-700 ring-brand-200",
  completed: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  failed: "bg-red-50 text-red-700 ring-red-200",
};

export function RunStateBadge({ state }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
        STATE_BADGE[state] ?? STATE_BADGE.running
      }`}
    >
      {state === "running" && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand-500" aria-hidden="true" />
      )}
      {state}
    </span>
  );
}

/**
 * Live log of one run: polls /api/runs/{runId}/events for new events about
 * once a second until the run ends, then stops.
 */
export default function RunLog({ runId, onFinished }) {
  const [run, setRun] = useState(null);
  const [events, setEvents] = useState([]);
  const [error, setError] = useState(null);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    let after = 0;
    setRun(null);
    setEvents([]);
    setError(null);

    const poll = async () => {
      try {
        const response = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/events?after=${after}`,
        );
        if (!response.ok) throw new Error(`Request failed with status ${response.status}`);
        const page = await response.json();
        if (cancelled) return;
        if (page.events.length) {
          after = page.events[page.events.length - 1].seq;
          setEvents((previous) => [...previous, ...page.events]);
        }
        setRun(page.run);
        setError(null);
        if (page.run.state === "running") {
          timer = setTimeout(poll, POLL_MS);
        } else {
          finishedRef.current?.(page.run);
        }
      } catch (caught) {
        if (cancelled) return;
        setError(caught.message);
        timer = setTimeout(poll, POLL_MS * 3);
      }
    };
    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [runId]);

  return (
    <div className="space-y-4" aria-live="polite">
      {run && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="min-w-0 text-sm font-medium text-slate-900 [overflow-wrap:anywhere]">{run.title}</p>
          <RunStateBadge state={run.state} />
        </div>
      )}
      {error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          Could not read the run log: {error}. Retrying…
        </p>
      )}
      <EventTimeline
        events={events}
        emptyText={run?.state === "running" || !run ? "Starting…" : "No steps were recorded."}
      />
      {run && run.state !== "running" && run.outcome && (
        <p
          className={`rounded-md px-3 py-2 text-sm font-medium [overflow-wrap:anywhere] ${
            run.state === "failed" ? "bg-red-50 text-red-800" : "bg-slate-100 text-slate-800"
          }`}
        >
          {run.outcome}
        </p>
      )}
    </div>
  );
}

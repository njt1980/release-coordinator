import { useEffect, useRef, useState } from "react";

const SUGGESTIONS = [
  "Why was the latest assessment blocked?",
  "What does makeline-service depend on, and how do we know?",
  "What does live_uncovered mean and how do I fix it?",
  "What do I need to set up a new project?",
];

// Renders an answer: fenced code blocks as <pre>, everything else as text
// with its line breaks kept. No HTML from the model is ever interpreted.
function AnswerText({ text }) {
  const parts = text.split(/```[a-zA-Z]*\n?/);
  return (
    <div className="space-y-2">
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <pre
            key={index}
            className="overflow-x-auto rounded-lg bg-ink-950 p-3 font-mono text-xs leading-relaxed text-ink-200"
          >
            {part.replace(/\n$/, "")}
          </pre>
        ) : (
          part.trim() && (
            <p key={index} className="whitespace-pre-wrap [overflow-wrap:anywhere]">
              {part.trim()}
            </p>
          )
        ),
      )}
    </div>
  );
}

function Message({ message }) {
  if (message.role === "user") {
    return (
      <div className="ml-8 rounded-2xl rounded-br-sm bg-brand-600 px-4 py-2.5 text-sm text-white">
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{message.text}</p>
      </div>
    );
  }
  if (message.role === "error") {
    return (
      <p role="alert" className="rounded-xl bg-red-50 px-4 py-2.5 text-sm text-red-700">
        {message.text}
      </p>
    );
  }
  return (
    <div className="mr-4 rounded-2xl rounded-bl-sm bg-white px-4 py-3 text-sm leading-relaxed text-slate-800 ring-1 ring-slate-200">
      <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-violet-700">AI-generated</p>
      <AnswerText text={message.text} />
      {message.tools?.length > 0 && (
        <p className="mt-2 border-t border-slate-100 pt-2 text-xs text-slate-500">
          Looked at:{" "}
          {[...new Set(message.tools.map((tool) => tool.name.replaceAll("_", " ")))].join(", ")}
        </p>
      )}
    </div>
  );
}

function SparkIcon({ className = "" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={className} aria-hidden="true">
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6" strokeLinecap="round" />
    </svg>
  );
}

/**
 * "Ask the platform": a drawer for questions about the platform's own
 * evidence. Answers come from read-only tools; nothing can be changed here.
 */
export default function AskPanel({ page, runId, raised = false }) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null);
  const listRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (!open || status) return;
    fetch("/api/assistant/status")
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => data && setStatus(data))
      .catch(() => {});
  }, [open, status]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => event.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const send = async (text) => {
    const question = text.trim();
    if (!question || busy) return;
    setDraft("");
    setMessages((previous) => [...previous, { role: "user", text: question }]);
    setBusy(true);
    try {
      const response = await fetch("/api/assistant/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: question,
          conversation_id: conversationId,
          context: { page, run_id: runId ?? null },
        }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = Array.isArray(data?.detail)
          ? data.detail.map((item) => item.msg).join("; ")
          : data?.detail;
        throw new Error(detail || `Request failed with status ${response.status}`);
      }
      setConversationId(data.conversation_id);
      setMessages((previous) => [
        ...previous,
        { role: "assistant", text: data.reply, tools: data.tools_used },
      ]);
    } catch (error) {
      setMessages((previous) => [...previous, { role: "error", text: error.message }]);
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setMessages([]);
    setConversationId(null);
  };

  return (
    <>
      {!open && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className={`fixed right-4 z-30 inline-flex items-center gap-2 rounded-full bg-ink-900 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-ink-950/30 ring-1 ring-white/10 transition-colors hover:bg-ink-800 sm:right-6 ${
            raised ? "bottom-36 sm:bottom-28" : "bottom-6"
          }`}
        >
          <SparkIcon className="h-4 w-4 text-signal-300" />
          Ask
        </button>
      )}

      {open && (
        <div className="fixed inset-0 z-40 flex justify-end bg-ink-950/30 backdrop-blur-[2px]" onClick={() => setOpen(false)}>
          <aside
            role="dialog"
            aria-modal="true"
            aria-labelledby="ask-heading"
            onClick={(event) => event.stopPropagation()}
            className="flex h-full w-full max-w-md flex-col bg-slate-50 shadow-2xl"
          >
            <header className="flex items-start justify-between gap-3 border-b border-slate-200 bg-white px-5 py-4">
              <div>
                <h2 id="ask-heading" className="flex items-center gap-2 text-base font-semibold text-slate-900">
                  <SparkIcon className="h-4 w-4 text-brand-600" />
                  Ask the platform
                </h2>
                <p className="mt-1 text-xs leading-relaxed text-slate-500">
                  Answers come from this platform's evidence. The assistant can explain and
                  draft, but can't approve or change anything. Questions are sent to
                  Anthropic's API{status?.model ? ` (${status.model})` : ""}.
                </p>
              </div>
              <div className="flex shrink-0 gap-1">
                {messages.length > 0 && (
                  <button
                    type="button"
                    onClick={reset}
                    className="rounded-md px-2 py-1 text-xs font-medium text-slate-500 hover:bg-slate-100 hover:text-slate-900"
                  >
                    New chat
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  aria-label="Close"
                  className="rounded-md px-2 py-1 text-lg leading-none text-slate-500 hover:bg-slate-100 hover:text-slate-900"
                >
                  ×
                </button>
              </div>
            </header>

            {status && !status.configured && (
              <p className="border-b border-amber-200 bg-amber-50 px-5 py-2.5 text-xs leading-relaxed text-amber-900">
                No Claude API key is set. Add <code className="font-mono">ANTHROPIC_API_KEY</code> to{" "}
                <code className="font-mono">.env</code> and restart the backend to enable answers.
              </p>
            )}

            <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto px-5 py-4" aria-live="polite">
              {messages.length === 0 ? (
                <div className="space-y-2">
                  <p className="text-sm text-slate-600">Try asking:</p>
                  {SUGGESTIONS.map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      onClick={() => send(suggestion)}
                      className="block w-full rounded-xl bg-white px-3 py-2.5 text-left text-sm text-slate-700 ring-1 ring-slate-200 hover:ring-brand-300"
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              ) : (
                messages.map((message, index) => <Message key={index} message={message} />)
              )}
              {busy && (
                <p className="flex items-center gap-2 text-sm text-slate-500">
                  <span className="h-2 w-2 animate-pulse rounded-full bg-brand-500" aria-hidden="true" />
                  Looking at the evidence…
                </p>
              )}
            </div>

            <form
              onSubmit={(event) => {
                event.preventDefault();
                send(draft);
              }}
              className="border-t border-slate-200 bg-white p-4"
            >
              <label htmlFor="ask-input" className="sr-only">
                Your question
              </label>
              <div className="flex items-end gap-2">
                <textarea
                  id="ask-input"
                  ref={inputRef}
                  rows={2}
                  maxLength={4000}
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      send(draft);
                    }
                  }}
                  placeholder="Ask about a run, a service, a policy…"
                  className="block w-full resize-none rounded-lg border-0 px-3 py-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-500"
                />
                <button
                  type="submit"
                  disabled={busy || !draft.trim()}
                  className="shrink-0 rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-500 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Send
                </button>
              </div>
            </form>
          </aside>
        </div>
      )}
    </>
  );
}

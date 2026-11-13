function ShieldIcon({ className = "" }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent"
    />
  );
}

function AccessTestResult({ testState }) {
  if (testState.status === "loading") {
    return (
      <p className="text-sm text-slate-600">Verifying read-only access&hellip;</p>
    );
  }
  if (testState.status !== "success" && testState.status !== "error") return null;
  const ok = testState.status === "success";
  return (
    <p
      role={ok ? "status" : "alert"}
      className={`min-w-0 flex-1 rounded-md px-3 py-2 text-sm ${
        ok ? "bg-green-50 text-green-800" : "bg-red-50 text-red-700"
      }`}
    >
      <span className="font-medium">{ok ? "\u2713 Access verified" : "\u2717 Access failed"}:</span>{" "}
      {testState.message}
    </p>
  );
}

/**
 * Card wrapper for one integration's onboarding inputs.
 * Surfaces the exact read-only role the integration requires, with a
 * justification aimed at security reviewers. When `onTest` is provided, the
 * card renders its own "Test Access" button and shows `testState`
 * ({ status: "idle" | "loading" | "success" | "error", message }).
 */
const STATUS_PILL = {
  success: { text: "Verified", className: "bg-emerald-50 text-emerald-700 ring-emerald-200" },
  error: { text: "Failed", className: "bg-red-50 text-red-700 ring-red-200" },
  loading: { text: "Testing", className: "bg-brand-50 text-brand-700 ring-brand-200" },
};

export default function ScopeInputCard({
  title,
  eyebrow,
  description,
  requiredRole,
  securityJustification,
  onTest,
  testState = { status: "idle", message: "" },
  testDisabled = false,
  children,
}) {
  const isTesting = testState.status === "loading";
  const pill = STATUS_PILL[testState.status];
  return (
    <section
      className={`rounded-2xl bg-white p-6 shadow-sm ring-1 transition-shadow sm:p-8 ${
        testState.status === "success" ? "ring-emerald-300" : "ring-slate-200"
      }`}
    >
      <header className="mb-5 flex items-start justify-between gap-4">
        <div>
          {eyebrow && (
            <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              {eyebrow}
            </p>
          )}
          <h3 className="mt-0.5 text-lg font-semibold text-slate-900">{title}</h3>
          {description && (
            <p className="mt-1 text-sm leading-relaxed text-slate-600">{description}</p>
          )}
        </div>
        {pill && (
          <span
            className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${pill.className}`}
          >
            {pill.text}
          </span>
        )}
      </header>

      <details className="group mb-5 rounded-lg bg-slate-50 px-4 py-3 ring-1 ring-inset ring-slate-200">
        <summary className="flex cursor-pointer list-none items-start gap-2 text-sm text-slate-700 [&::-webkit-details-marker]:hidden">
          <ShieldIcon className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
          <span className="min-w-0 flex-1">
            <span className="font-medium text-slate-900">Read-only access:</span> {requiredRole}
          </span>
          {securityJustification && (
            <span className="shrink-0 text-xs font-medium text-brand-600 group-open:hidden">Why?</span>
          )}
        </summary>
        {securityJustification && (
          <p className="mt-2 pl-6 text-xs leading-relaxed text-slate-500">
            {securityJustification}
          </p>
        )}
      </details>

      <div className="space-y-4">{children}</div>

      {onTest && (
        <div
          aria-live="polite"
          className="mt-6 flex flex-col gap-3 border-t border-slate-100 pt-5 sm:flex-row sm:items-start sm:justify-between"
        >
          <AccessTestResult testState={testState} />
          <button
            type="button"
            onClick={onTest}
            disabled={isTesting || testDisabled}
            className="inline-flex shrink-0 items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-brand-700 ring-1 ring-inset ring-brand-200 hover:bg-brand-50 disabled:cursor-not-allowed disabled:opacity-50 sm:ml-auto"
          >
            {isTesting && <Spinner />}
            Test Access
          </button>
        </div>
      )}
    </section>
  );
}

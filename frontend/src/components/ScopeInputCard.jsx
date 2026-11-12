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
export default function ScopeInputCard({
  title,
  description,
  requiredRole,
  securityJustification,
  onTest,
  testState = { status: "idle", message: "" },
  testDisabled = false,
  children,
}) {
  const isTesting = testState.status === "loading";
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <header className="mb-4">
        <h2 className="text-lg font-semibold text-slate-900">{title}</h2>
        {description && (
          <p className="mt-1 text-sm text-slate-600">{description}</p>
        )}
      </header>

      <div className="mb-5 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3">
        <div className="flex items-center gap-2 text-sm font-medium text-emerald-800">
          <ShieldIcon className="h-4 w-4 shrink-0" />
          <span>
            Required access: <span className="font-semibold">{requiredRole}</span>
          </span>
        </div>
        {securityJustification && (
          <p className="mt-1.5 pl-6 text-xs leading-relaxed text-slate-500">
            {securityJustification}
          </p>
        )}
      </div>

      <div className="space-y-4">{children}</div>

      {onTest && (
        <div
          aria-live="polite"
          className="mt-5 flex flex-col gap-3 border-t border-slate-100 pt-4 sm:flex-row sm:items-start sm:justify-between"
        >
          <AccessTestResult testState={testState} />
          <button
            type="button"
            onClick={onTest}
            disabled={isTesting || testDisabled}
            className="inline-flex shrink-0 items-center justify-center gap-2 rounded-md border border-indigo-600 px-4 py-2 text-sm font-semibold text-indigo-600 hover:bg-indigo-50 disabled:cursor-not-allowed disabled:opacity-50 sm:ml-auto"
          >
            {isTesting && <Spinner />}
            Test Access
          </button>
        </div>
      )}
    </section>
  );
}

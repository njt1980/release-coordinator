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

/**
 * Card wrapper for one integration's onboarding inputs.
 * Surfaces the exact read-only role the integration requires, with a
 * justification aimed at security reviewers.
 */
export default function ScopeInputCard({
  title,
  description,
  requiredRole,
  securityJustification,
  children,
}) {
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
    </section>
  );
}

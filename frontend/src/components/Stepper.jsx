/**
 * Progress through onboarding. Each step is { label, detail, done }; the
 * first step that is not done is the current one.
 */
export default function Stepper({ steps }) {
  const current = steps.findIndex((step) => !step.done);
  return (
    <ol aria-label="Onboarding progress" className="grid grid-cols-3 gap-2 sm:gap-3">
      {steps.map((step, index) => {
        const state = step.done ? "done" : index === current ? "current" : "upcoming";
        return (
          <li
            key={step.label}
            aria-current={state === "current" ? "step" : undefined}
            className={`rounded-xl px-3 py-3 ring-1 transition-colors sm:px-4 ${
              state === "done"
                ? "bg-emerald-50/60 ring-emerald-200"
                : state === "current"
                  ? "bg-white shadow-sm ring-brand-300"
                  : "bg-white/60 ring-slate-200"
            }`}
          >
            <div className="flex items-center gap-2">
              <span
                className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ${
                  state === "done"
                    ? "bg-emerald-600 text-white"
                    : state === "current"
                      ? "bg-brand-600 text-white"
                      : "bg-slate-200 text-slate-600"
                }`}
                aria-hidden="true"
              >
                {state === "done" ? "✓" : index + 1}
              </span>
              <span className="truncate text-sm font-medium text-slate-900">{step.label}</span>
              <span className="sr-only">
                {state === "done" ? "(complete)" : state === "current" ? "(current)" : ""}
              </span>
            </div>
            <p className="mt-1 hidden truncate pl-7 text-xs text-slate-500 sm:block">{step.detail}</p>
          </li>
        );
      })}
    </ol>
  );
}

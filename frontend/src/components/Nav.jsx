import { ROUTES, href } from "../lib/router.js";

export function LogoMark({ className = "" }) {
  // Three services and the order they release in.
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <rect width="32" height="32" rx="8" className="fill-brand-600" />
      <path d="M9 22 L16 10 L23 22" fill="none" stroke="white" strokeWidth="2" strokeOpacity="0.55" />
      <circle cx="9" cy="22" r="3.2" fill="white" />
      <circle cx="16" cy="10" r="3.2" className="fill-signal-300" />
      <circle cx="23" cy="22" r="3.2" fill="white" />
    </svg>
  );
}

const LINKS = [
  // Narrow screens keep the working pages; the logo links home, and the
  // landing page links to How it works.
  { path: ROUTES.home, label: "Overview", wide: true },
  { path: ROUTES.howItWorks, label: "How it works", wide: true },
  { path: ROUTES.review, label: "Review" },
  { path: ROUTES.assessments, label: "Assessments" },
  { path: ROUTES.runs, label: "Runs" },
];

export default function Nav({ path, tone = "dark" }) {
  const dark = tone === "dark";
  const linkClass = (active) =>
    `whitespace-nowrap rounded-md px-2 py-2 text-sm font-medium transition-colors sm:px-3 ${
      dark
        ? active
          ? "text-white"
          : "text-ink-300 hover:text-white"
        : active
          ? "text-slate-900"
          : "text-slate-500 hover:text-slate-900"
    }`;
  return (
    <header
      className={
        dark
          ? "relative z-10"
          : "sticky top-0 z-20 border-b border-slate-200 bg-white/85 backdrop-blur"
      }
    >
      <nav
        aria-label="Main"
        className="mx-auto flex max-w-6xl items-center justify-between gap-2 px-4 py-4 sm:px-6"
      >
        <a
          href={href(ROUTES.home)}
          className={`flex items-center gap-2.5 font-semibold tracking-tight ${
            dark ? "text-white" : "text-slate-900"
          }`}
        >
          <LogoMark className="h-8 w-8" />
          <span className="hidden sm:inline">Release Coordinator</span>
        </a>
        <div className="flex items-center gap-1">
          {LINKS.map((link) => (
            <a
              key={link.path}
              href={href(link.path)}
              aria-current={path === link.path ? "page" : undefined}
              className={`${linkClass(path === link.path)} ${link.wide ? "hidden sm:inline-block" : ""}`}
            >
              {link.label}
            </a>
          ))}
          <a
            href={href(ROUTES.onboard)}
            aria-current={path === ROUTES.onboard ? "page" : undefined}
            className="ml-1 shrink-0 rounded-md bg-brand-600 px-3 py-2 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-brand-500"
          >
            Onboard
          </a>
        </div>
      </nav>
    </header>
  );
}

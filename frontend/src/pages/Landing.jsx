import DependencyGraphHero, { PHASES, useHeroPhase } from "../components/DependencyGraphHero.jsx";
import Nav from "../components/Nav.jsx";
import Footer from "../components/Footer.jsx";
import { ROUTES, href } from "../lib/router.js";

// Copy follows docs/release-model.md; keep claims within what it states.
const PROBLEMS = [
  {
    title: "Dependencies hide in configuration",
    body: "Services find each other through DNS names, environment variables and ConfigMaps. The architecture diagram was right once.",
  },
  {
    title: "Production disagrees with the plan",
    body: "Calls that nobody wrote down show up in traffic, and calls everybody remembers stopped months ago. Only observed traffic settles it.",
  },
  {
    title: "Release order is a dependency problem",
    body: "Additive changes ship providers first; breaking changes ship consumers first. Get it backwards and the release becomes the incident.",
  },
];

const STEPS = [
  {
    title: "Discover",
    body: "Read the code, the Kubernetes manifests and the live traffic, using read-only access.",
  },
  {
    title: "Baseline",
    body: "Merge the evidence into one versioned map of services, dependencies and gaps.",
  },
  {
    title: "Assess",
    body: "Map a pull request's files to services and walk every caller and dependency.",
  },
  {
    title: "Decide",
    body: "Give a deterministic order and a verdict with reason codes. The model can only say no.",
  },
];

const SOURCES = [
  { name: "Code", detail: "Azure DevOps repositories", note: "Service boundaries and the addresses each service uses" },
  { name: "Manifests", detail: "Kubernetes namespaces", note: "Services, workloads, ConfigMaps; never Secrets" },
  { name: "Traffic", detail: "Azure Monitor telemetry", note: "Your instrumentation, or our eBPF agent with no code changes" },
];

function Hero() {
  const phase = useHeroPhase();
  return (
    <section className="relative overflow-hidden bg-ink-950 text-white">
      <Nav path={ROUTES.home} tone="dark" />
      <div className="mx-auto grid max-w-6xl items-center gap-10 px-4 pb-16 pt-8 sm:px-6 lg:grid-cols-[1fr_1.1fr] lg:pb-24 lg:pt-14">
        <div className="animate-fade-up">
          <p className="inline-flex items-center gap-2 rounded-full border border-ink-700 bg-ink-900 px-3 py-1 text-xs font-medium text-ink-200">
            <span className="h-1.5 w-1.5 rounded-full bg-signal-400" aria-hidden="true" />
            Pilot for Kubernetes microservices
          </p>
          <h1 className="mt-6 text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">
            Know what a release touches{" "}
            <span className="bg-gradient-to-r from-brand-300 to-signal-300 bg-clip-text text-transparent">
              before it ships.
            </span>
          </h1>
          <p className="mt-5 max-w-xl text-lg leading-relaxed text-ink-200">
            Release Coordinator maps how your services really depend on each other,
            from code, manifests and live traffic, and checks every pull request
            against that map. It reads; it never changes your code or your cluster.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <a
              href={href(ROUTES.onboard)}
              className="rounded-md bg-brand-500 px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-brand-900/40 transition-colors hover:bg-brand-400"
            >
              Onboard a project
            </a>
            <a
              href={href(ROUTES.howItWorks)}
              className="rounded-md border border-ink-700 px-5 py-2.5 text-sm font-semibold text-ink-200 transition-colors hover:border-ink-300 hover:text-white"
            >
              How it works
            </a>
          </div>
        </div>

        <figure className="rounded-2xl border border-ink-800 bg-ink-950/60 p-3 shadow-2xl shadow-black/40 ring-1 ring-white/5">
          <DependencyGraphHero phase={phase} />
          <figcaption className="px-2 pb-1 pt-3">
            <ol className="grid gap-1 text-sm" aria-label="What the animation shows">
              {PHASES.map((text, index) => (
                <li
                  key={text}
                  aria-current={index === phase ? "step" : undefined}
                  className={`flex items-start gap-2 transition-colors duration-500 ${
                    index === phase ? "text-white" : "text-ink-300/60"
                  }`}
                >
                  <span
                    className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${
                      index === phase ? "bg-signal-400" : "bg-ink-700"
                    }`}
                    aria-hidden="true"
                  />
                  {text}
                </li>
              ))}
            </ol>
          </figcaption>
        </figure>
      </div>
    </section>
  );
}

function SectionHeading({ eyebrow, title, children }) {
  return (
    <div className="max-w-2xl">
      <p className="text-sm font-semibold uppercase tracking-wider text-brand-600">{eyebrow}</p>
      <h2 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">{title}</h2>
      {children && <p className="mt-4 text-base leading-relaxed text-slate-600">{children}</p>}
    </div>
  );
}

export default function Landing() {
  return (
    <>
      <Hero />
      <main>
        <section className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
          <SectionHeading eyebrow="The problem" title="Every release is a bet on a map nobody maintains">
            In a microservices system the risk of a change sits in what calls it and
            what it calls. That information is spread across repositories, cluster
            configuration and traffic, and it is out of date somewhere.
          </SectionHeading>
          <div className="mt-12 grid gap-6 md:grid-cols-3">
            {PROBLEMS.map((problem) => (
              <article
                key={problem.title}
                className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"
              >
                <h3 className="text-lg font-semibold text-slate-900">{problem.title}</h3>
                <p className="mt-3 text-sm leading-relaxed text-slate-600">{problem.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="border-y border-slate-200 bg-white">
          <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
            <SectionHeading eyebrow="How it decides" title="Evidence first, then a verdict you can audit" />
            <ol className="mt-12 grid gap-8 md:grid-cols-4">
              {STEPS.map((step, index) => (
                <li key={step.title} className="relative">
                  <span className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-50 font-mono text-sm font-semibold text-brand-700 ring-1 ring-brand-200">
                    {index + 1}
                  </span>
                  <h3 className="mt-4 text-base font-semibold text-slate-900">{step.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-slate-600">{step.body}</p>
                </li>
              ))}
            </ol>
            <div className="mt-14 grid gap-4 md:grid-cols-3">
              {SOURCES.map((source) => (
                <div key={source.name} className="rounded-xl bg-slate-50 p-5 ring-1 ring-slate-200">
                  <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                    {source.name}
                  </p>
                  <p className="mt-1 font-semibold text-slate-900">{source.detail}</p>
                  <p className="mt-1 text-sm text-slate-600">{source.note}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-6 sm:p-8">
            <h2 className="text-lg font-semibold text-amber-950">Where it stands today</h2>
            <p className="mt-3 max-w-3xl text-sm leading-relaxed text-amber-950/80">
              This is a pilot. Its verdicts are advisory and reviewed by an engineer.
              No release is approved yet, because the platform doesn&apos;t yet compare
              API and message contracts (<span className="font-mono">COMPATIBILITY_UNVERIFIED</span>).
              It supports Azure DevOps, Kubernetes and Azure Monitor today, and the
              platform&apos;s current limits are listed alongside how it works.
            </p>
            <a
              href={href(ROUTES.howItWorks)}
              className="mt-5 inline-block text-sm font-semibold text-amber-900 underline decoration-amber-400 underline-offset-4 hover:decoration-amber-700"
            >
              Read how it works and its limits
            </a>
          </div>
        </section>
      </main>
      <Footer />
    </>
  );
}

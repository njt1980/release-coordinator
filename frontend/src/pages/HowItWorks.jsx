import Nav from "../components/Nav.jsx";
import Footer from "../components/Footer.jsx";
import { ROUTES, href } from "../lib/router.js";

// Sources of truth: docs/release-model.md (sections 1-6) and
// docs/access-requirements.md. When those change, update this page.

const SECTIONS = [
  { id: "solves", label: "What it solves" },
  { id: "decides", label: "How a release is decided" },
  { id: "reasons", label: "Blocking reasons" },
  { id: "setup", label: "Set it up" },
  { id: "intended-use", label: "Intended use" },
  { id: "limits", label: "Current limits" },
];

const DECISION_STEPS = [
  ["Policy", "Load the project's policy. A project without a valid policy is never approved."],
  ["Mapping", "Map the pull request's changed files to services by the directories they own."],
  ["Radius", "Walk the baseline from the changed services to every upstream caller and downstream dependency."],
  ["Live check", "Query telemetry for the same workspace. An edge in the radius that the baseline lacks means the baseline has drifted."],
  ["Sequence", "Order the deployment deterministically: providers first for additive changes, consumers first for breaking ones."],
  ["Risk", "A model writes an advisory assessment. It can reject a release but never approve one the deterministic checks block."],
  ["Verdict", "Approved only when no blocking reason applies; otherwise every reason is listed."],
];

const REASONS = [
  ["POLICY_MISSING, POLICY_INVALID", "No usable policy for the project"],
  ["BASELINE_UNAVAILABLE, BASELINE_STALE, BASELINE_SCOPE_MISMATCH", "No baseline, too old, or not provably for the policy's environment and cluster"],
  ["REQUIRED_SOURCE_MISSING", "A discovery source the policy requires did not succeed"],
  ["UNMAPPED_FILES", "Changed files that no service owns and the policy doesn't exempt"],
  ["DISCOVERY_GAP", "An unresolved dependency in the release radius"],
  ["BASELINE_DRIFT", "Live telemetry shows an edge in the radius that the baseline lacks"],
  ["DEPENDENCY_CYCLE", "No safe deployment order exists"],
  ["COMPATIBILITY_UNVERIFIED", "No contract evidence proves the change compatible (only .proto contracts are read; by default only contract-only pull requests can be verified)"],
  ["CONTRACT_BREAKING", "A changed .proto contract breaks compatibility; the verdict stays actionable and a person decides"],
  ["DEMO_EVIDENCE, MODEL_REJECTED", "Mock data was used, or the risk model rejected the release"],
];

const ACCESS = [
  {
    system: "Azure DevOps",
    grant: "Organization user (Basic) in the project's Readers group",
    note: "Reads repositories, files and build definitions. Personal Microsoft accounts don't work; use a service principal or managed identity.",
  },
  {
    system: "Kubernetes",
    grant: "Namespaced Role: get and list on services, configmaps, pods, deployments and statefulsets",
    note: "Bound per namespace. Secrets are never read.",
  },
  {
    system: "Log Analytics",
    grant: "Log Analytics Reader on the workspace that holds AppDependencies",
    note: "Application Insights must be workspace-based.",
  },
];

const POLICY_SAMPLE = `policy_version: 3
tenant: acme
project_id: shop
environment: prod
cluster: aks-prod-1
max_baseline_age_hours: 24
required_sources: [STATIC_CODE, K8S_MANIFEST, RUNTIME_TELEMETRY]
runtime_evidence: live
# What proves the cluster label above
kubernetes_api_server: https://aks-prod-1.hcp.eastus.azmk8s.io:443
telemetry_cluster:
  attribute: k8s.cluster.name
  value: aks-prod-1
# or, for Application Insights SDKs that write no cluster attribute:
# telemetry_components:
#   - /subscriptions/<id>/resourceGroups/<rg>/providers/microsoft.insights/components/<name>
exemptions:
  files:
    - pattern: "docs/**"
      reason: "Documentation only"
  dependencies: []`;

// What the platform is not (docs/intended-use.md is the source of these claims).
const INTENDED_USE_LIMITS = [
  "Not a medical device, and not part of one.",
  "Not validated software: the customer validates it for its own intended use.",
  "Not compliant with 21 CFR Part 11, IEC 62304, ISO 13485 or the MDR by itself; the decision record is designed to support them.",
  "Not an approver: it never releases or blocks a deployment. The decision is the person's.",
  "Not a complete impact analysis: the blast radius covers what the evidence shows, and the briefing lists what it couldn't see.",
  "Not a risk assessment under ISO 14971: the advisory risk review is AI-generated text that supports the person's judgement.",
];

const LIMITS = [
  "Verdicts are advisory and reviewed by an engineer; nothing is approved until contract analysis exists.",
  "Code from Azure DevOps and telemetry from Azure Monitor only; GitHub, GitLab and other telemetry backends need adapters.",
  "Messaging is modeled as service to broker, not producer to queue or topic to consumer.",
  "The backend runs as a single process without authentication: local pilot use only.",
  "The eBPF agent bundle is a lab deployment, not yet a parameterized package.",
  "Applications outside Kubernetes, executing deployments and database migrations are out of scope.",
];

function Section({ id, title, children }) {
  return (
    <section id={id} aria-labelledby={`${id}-heading`} className="scroll-mt-24 border-t border-slate-200 py-12 first:border-t-0 first:pt-0">
      <h2 id={`${id}-heading`} className="text-2xl font-semibold tracking-tight text-slate-900">
        {title}
      </h2>
      <div className="mt-5 space-y-4 text-base leading-relaxed text-slate-600">{children}</div>
    </section>
  );
}

function SetupStep({ number, title, children }) {
  return (
    <li className="relative pl-12">
      <span className="absolute left-0 top-0 flex h-8 w-8 items-center justify-center rounded-full bg-brand-600 font-mono text-sm font-semibold text-white">
        {number}
      </span>
      <h3 className="text-lg font-semibold text-slate-900">{title}</h3>
      <div className="mt-2 space-y-3 text-sm leading-relaxed text-slate-600">{children}</div>
    </li>
  );
}

export default function HowItWorks() {
  // In-page links use buttons: the URL hash is reserved for routing.
  const jumpTo = (id) => document.getElementById(id)?.scrollIntoView({ block: "start" });
  return (
    <>
      <Nav path={ROUTES.howItWorks} tone="light" />
      <main className="mx-auto max-w-6xl px-4 py-12 sm:px-6 lg:py-16">
        <header className="max-w-3xl">
          <p className="text-sm font-semibold uppercase tracking-wider text-brand-600">How it works</p>
          <h1 className="mt-2 text-4xl font-semibold tracking-tight text-slate-900">
            From scattered evidence to a release order you can defend
          </h1>
          <p className="mt-4 text-lg leading-relaxed text-slate-600">
            What the platform solves, how it reaches a verdict, and what a team needs
            to set it up. It works for any microservices application on Kubernetes
            and never needs to change the application&apos;s code, images or manifests.
          </p>
        </header>

        <div className="mt-12 lg:grid lg:grid-cols-[200px_1fr] lg:gap-12">
          <nav aria-label="On this page" className="hidden lg:block">
            <ul className="sticky top-24 space-y-1 text-sm">
              {SECTIONS.map((section) => (
                <li key={section.id}>
                  <button
                    type="button"
                    onClick={() => jumpTo(section.id)}
                    className="w-full rounded-md px-3 py-1.5 text-left text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                  >
                    {section.label}
                  </button>
                </li>
              ))}
            </ul>
          </nav>

          <div className="min-w-0">
            <Section id="solves" title="What it solves">
              <p>
                In a microservices system, a change is only as safe as everything that
                calls it and everything it calls. That map is spread across source code,
                cluster configuration and live traffic, and each of them is incomplete
                on its own.
              </p>
              <p>
                Release Coordinator builds the map from all three, keeps it as a
                versioned <strong className="text-slate-900">baseline</strong>, and
                checks each pull request against it: which services are affected, in
                what order they can ship, and every reason it isn&apos;t safe yet.
              </p>
            </Section>

            <Section id="decides" title="How a release is decided">
              <p>
                Onboarding runs three read-only scans (code, manifests, telemetry) and
                merges them into a baseline snapshot that records exactly what was
                scanned. Every service gets one identity,{" "}
                <code className="break-all rounded bg-slate-100 px-1.5 py-0.5 text-sm text-slate-800">
                  urn:release-coord:tenant:environment:cluster:namespace:name
                </code>
                , and anything that matches no single service is kept as an unresolved
                entity rather than guessed. Each pull request is then assessed:
              </p>
              <ol className="mt-6 grid gap-3 sm:grid-cols-2">
                {DECISION_STEPS.map(([title, body], index) => (
                  <li key={title} className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
                    <p className="font-mono text-xs font-semibold text-brand-600">
                      {String(index + 1).padStart(2, "0")}
                    </p>
                    <p className="mt-1 font-semibold text-slate-900">{title}</p>
                    <p className="mt-1 text-sm text-slate-600">{body}</p>
                  </li>
                ))}
              </ol>
            </Section>

            <Section id="reasons" title="Blocking reasons">
              <p>
                A verdict always carries its reasons. Each of these makes an assessment
                non-actionable.
              </p>
              <div className="overflow-hidden rounded-xl ring-1 ring-slate-200">
                <table className="w-full text-left text-sm">
                  <thead className="bg-slate-50 text-slate-500">
                    <tr>
                      <th scope="col" className="px-4 py-3 font-medium">Code</th>
                      <th scope="col" className="px-4 py-3 font-medium">Meaning</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-200 bg-white">
                    {REASONS.map(([codes, meaning]) => (
                      <tr key={codes}>
                        <td className="px-4 py-3 align-top font-mono text-xs text-slate-800 [overflow-wrap:anywhere]">
                          {codes}
                        </td>
                        <td className="px-4 py-3 text-slate-600">{meaning}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>

            <Section id="setup" title="Set it up">
              <ol className="mt-2 space-y-10">
                <SetupStep number="1" title="Grant read-only access to a non-personal identity">
                  <p>
                    Locally, a service principal (its credentials go in a gitignored{" "}
                    <code>.env</code>); on Azure, a managed identity.
                  </p>
                  <ul className="grid gap-3 md:grid-cols-3">
                    {ACCESS.map((item) => (
                      <li key={item.system} className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
                        <p className="font-semibold text-slate-900">{item.system}</p>
                        <p className="mt-1 text-slate-700">{item.grant}</p>
                        <p className="mt-2 text-xs text-slate-500">{item.note}</p>
                      </li>
                    ))}
                  </ul>
                </SetupStep>
                <SetupStep number="2" title="Make dependency telemetry available">
                  <p>
                    Either use instrumentation the team already has (the OpenTelemetry
                    SDK or Application Insights), or install the telemetry agent bundle:
                    an eBPF agent (Beyla) and an OpenTelemetry Collector that need no
                    code changes. Rows must carry a cluster attribute, such as{" "}
                    <code>k8s.cluster.name</code>, so a shared workspace stays scoped.
                  </p>
                </SetupStep>
                <SetupStep number="3" title="Write the project policy">
                  <p>
                    A reviewed YAML file in the repository at{" "}
                    <code>policies/&lt;tenant&gt;/&lt;project&gt;.yaml</code>. It names the one
                    environment and cluster the project releases to, what proves that
                    cluster, the evidence required, and every exemption with its reason.
                  </p>
                  <pre className="overflow-x-auto rounded-xl bg-ink-950 p-4 font-mono text-xs leading-relaxed text-ink-200">
                    {POLICY_SAMPLE}
                  </pre>
                </SetupStep>
                <SetupStep number="4" title="Onboard and build the first baseline">
                  <p>
                    Enter the release target, test access for each source, and start the
                    crawl. From then on, pull request webhooks are assessed against the
                    newest complete baseline.
                  </p>
                  <a
                    href={href(ROUTES.onboard)}
                    className="inline-block rounded-md bg-brand-600 px-4 py-2 font-semibold text-white hover:bg-brand-500"
                  >
                    Go to onboarding
                  </a>
                </SetupStep>
              </ol>
            </Section>

            <Section id="intended-use" title="Intended use">
              <p>
                Release Coordinator helps a person decide whether, and in which order, to release a change to a
                microservices application. It gathers the evidence, shows which services the change can affect
                and a deployment order, and lists what the evidence cannot show. A named person makes and records
                the decision.
              </p>
              <ul className="space-y-2">
                {INTENDED_USE_LIMITS.map((item) => (
                  <li key={item} className="flex gap-3">
                    <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-400" aria-hidden="true" />
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
              <p>
                Every part of a briefing is labelled <strong>Computed</strong> (deterministic code over recorded
                evidence), <strong>Human</strong> (entered by a named person) or <strong>AI-generated</strong>{" "}
                (written by an external model; it may be wrong and is never part of a signed decision).
              </p>
            </Section>

            <Section id="limits" title="Current limits">
              <ul className="space-y-2">
                {LIMITS.map((limit) => (
                  <li key={limit} className="flex gap-3">
                    <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" aria-hidden="true" />
                    <span>{limit}</span>
                  </li>
                ))}
              </ul>
            </Section>
          </div>
        </div>
      </main>
      <Footer />
    </>
  );
}

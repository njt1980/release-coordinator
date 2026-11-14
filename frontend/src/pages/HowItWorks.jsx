import Nav from "../components/Nav.jsx";
import Footer from "../components/Footer.jsx";
import { ROUTES, href } from "../lib/router.js";

// Sources of truth: docs/release-model.md (sections 1-6),
// docs/access-requirements.md, docs/records-retention.md and the code paths
// named below. When those change, update this page.

const SECTIONS = [
  { id: "solves", label: "What it solves" },
  { id: "flows", label: "The two flows" },
  { id: "search", label: "How each source is searched" },
  { id: "merge", label: "Merging into one graph" },
  { id: "storage", label: "What is stored where" },
  { id: "decides", label: "How a release is decided" },
  { id: "reasons", label: "Reason codes" },
  { id: "setup", label: "Set it up" },
  { id: "intended-use", label: "Intended use" },
  { id: "limits", label: "Current limits" },
];

const DECISION_STEPS = [
  ["Policy", "Load the project's policy. A project without a valid policy is never approved.", "POLICY_MISSING · POLICY_INVALID"],
  ["Mapping", "Map each changed file to the service whose directory is deepest; a shared library's files belong to every service using it.", "UNMAPPED_FILES"],
  ["Revisions", "With Azure DevOps: the base is an ancestor of the head, the file list matches the commits, the baseline's scanned commit relates to the base. The changed services are re-scanned at the head, so the dependencies that will ship are assessed.", "REVISION_UNVERIFIED · BASELINE_REVISION_MISMATCH"],
  ["Review", "Apply the dependency review's answers; an answer counts only while the files it rests on are unchanged.", "REVIEW_UNAVAILABLE"],
  ["Radius", "Walk every upstream caller and downstream dependency, counting hops. Check every unresolved node, the baseline's age and scope, required sources, unlinked identities, and code coverage across the whole project (an unread file anywhere could be an unseen caller).", "DISCOVERY_GAP · BASELINE_STALE · STATIC_COVERAGE_INCOMPLETE · IDENTITY_UNLINKED"],
  ["Live check", "Query telemetry now: every service in the radius must be seen within the policy's window, and a live edge the baseline lacks is drift.", "REQUIRED_SOURCE_MISSING · BASELINE_DRIFT · RUNTIME_UNOBSERVABLE"],
  ["Contracts", "Compare changed .proto files at base and head. A breaking difference blocks; a compatible one verifies the order when nothing else changed (or every caller shares a contract, if the policy allows).", "CONTRACT_BREAKING · COMPATIBILITY_UNVERIFIED"],
  ["Sequence", "Order the deployment deterministically (topological sort): providers first for additive changes, consumers first for breaking ones. A cycle is named.", "DEPENDENCY_CYCLE"],
  ["Risk", "Only with complete evidence and external AI allowed: a model writes an advisory review of the redacted change. It can reject a release, never approve one.", "MODEL_REJECTED · ADVISORY_DISABLED"],
  ["Verdict", "Computed in code. The briefing groups issues by cause and labels every item computed, human or AI-generated; a named person records the decision.", ""],
];

const SOURCES = [
  {
    name: "Code",
    where: "Azure DevOps, read at one pinned commit",
    color: "bg-indigo-500",
    reads: [
      "The default branch head, resolved to a commit; every file fetched pinned to it (429/503 retried, blobs cached by git object id).",
      "Service boundaries from release-coord.yaml, else from Dockerfile, package.json or go.mod markers and runnable build projects (.csproj, pom.xml, build.gradle). A file outside every service that Dockerfiles COPY in (a shared .proto, say) belongs to each service that copies it.",
      "Dependencies: literal URLs to in-cluster or private hosts; reads of address-like environment variables (*_ADDR, *_URL, *_HOST, ConnectionStrings__*) in Python, JS/TS, Go, Java, C#, PHP and Ruby; appsettings and Spring config keys; Dockerfile ENV; nginx and Envoy upstreams; Feign and Eureka names; gRPC <Protobuf> contract links.",
    ],
    flags: "Flagged instead of guessed: dynamic variable names and config keys, client addresses built at runtime, a client library with no address anywhere, external config servers, unsupported languages (C/C++, Kotlin, Rust, Elixir, Scala…).",
  },
  {
    name: "Cluster",
    where: "Kubernetes API, per onboarded namespace",
    color: "bg-emerald-500",
    reads: [
      "Services, ConfigMaps, Deployments, StatefulSets, DaemonSets, ReplicaSets, CronJobs, Jobs and Pods. Secrets are never read.",
      "Container environments in Kubernetes' own precedence: literal values, configMapKeyRef, envFrom ConfigMaps. Secret references stay opaque names.",
      "An environment variable whose value names another Service becomes a dependency (unless it comes in bulk from a shared ConfigMap). The API server URL is recorded as proof of which cluster was read.",
    ],
    flags: "Recorded as findings: Services that select nothing, workloads under controllers it can't read, ExternalName Services, name collisions.",
  },
  {
    name: "Telemetry",
    where: "Application Insights / Log Analytics",
    color: "bg-amber-500",
    reads: [
      "AppDependencies, scoped by Application Insights component and/or a cluster attribute from the policy; the caller is service.name (or the role name).",
      "The request join: a call's Id matched to the ParentId of the receiving service's AppRequests row names the callee, even when the call's Target is an IP or a span name.",
      "sum(ItemCount) counts sampled calls; a source with ItemCount above 1 is marked sampled. Classic targets (host | cid, tcp:host,1433, host/entity) are parsed.",
    ],
    flags: "Dropped and counted: targets with no host (an HTTP method, a path), loopback calls, messaging rows whose direction is reversed.",
  },
];

const RESOLUTION = [
  ["Kubernetes sets it", "the manifest's value wins"],
  ["Something sets it unreadably", "a Secret, a pipeline variable or an image ARG: opaque, never guessed"],
  ["Dockerfile ENV", "of the final build stage"],
  ["Config files", "active profile, then base file, then the default written in code (each default used is recorded)"],
  ["Nothing sets it", "provably unset: the code default decides, so a person is asked"],
];

const STORES = [
  ["Baseline snapshot", "baselines/{tenant}/{project}/{snapshot}.json", "Services, dependencies, unresolved endpoints, what was scanned (cluster, API server, commits, telemetry scope), container environments, per-service coverage, contract links", "Never. Written atomically; a new discovery writes a new file."],
  ["Policy", "policies/{tenant}/{project}.yaml", "Reviewed rules and exemptions; each assessment records its version and SHA-256", "Edited by people, never by the platform"],
  ["Review answers", "reviews/reviews.sqlite3", "Confirm, disagree, add, link and answer, each with reviewer, reason and the files it rests on", "Never: append-only, SHA-256 chained"],
  ["Assessment", "assessments/{job}.json", "Verdict, reason codes, issues, the full evidence record and the briefing", "Written once (needs RC_DATA_DIR)"],
  ["Decision record", "decisions/decisions.sqlite3", "Verdict, reason, signer, UTC time, the assessment's SHA-256, commit, baseline and policy hash", "Never: append-only, SHA-256 chained; corrections are new records"],
];

// [codes, meaning, blocks actionability]
const REASONS = [
  ["POLICY_MISSING, POLICY_INVALID", "No usable policy for the project", true],
  ["BASELINE_UNAVAILABLE, BASELINE_STALE, BASELINE_SCOPE_MISMATCH", "No baseline, too old, or not provably for the policy's environment and cluster", true],
  ["REQUIRED_SOURCE_MISSING", "A required source did not succeed, or runtime evidence is weaker than required", true],
  ["UNMAPPED_FILES", "Changed files that no service owns and the policy doesn't exempt", true],
  ["DISCOVERY_GAP", "An unresolved dependency in the release radius", true],
  ["BASELINE_DRIFT", "Live telemetry shows an edge in the radius that the baseline lacks", true],
  ["REVISION_UNVERIFIED, BASELINE_REVISION_MISMATCH", "The pull request's commits can't be verified, or the baseline doesn't match its base", true],
  ["STATIC_COVERAGE_INCOMPLETE", "Source files the scanner couldn't read or understand", true],
  ["IDENTITY_UNLINKED, RUNTIME_UNOBSERVABLE", "Code that matches no running service, or a service the telemetry can't see", true],
  ["DEPENDENCY_CYCLE", "No safe deployment order exists", true],
  ["COMPATIBILITY_UNVERIFIED", "No contract evidence proves the change compatible (only .proto contracts are read; by default only contract-only pull requests can be verified)", true],
  ["REVIEW_UNAVAILABLE, DEMO_EVIDENCE", "The dependency review couldn't be read, or mock data was used", true],
  ["CONTRACT_BREAKING", "A changed .proto contract breaks compatibility: a No-go on complete evidence", false],
  ["MODEL_REJECTED", "The advisory risk review rejected the release", false],
  ["ADVISORY_DISABLED", "External AI is off for the project; a person decides", false],
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
  "Verdicts are advisory; a named person decides. A pull request that changes code can't be approved automatically under the default strict contract scope.",
  "Contracts: only .proto files are compared, and client links are recorded for .NET builds only. OpenAPI and message schemas aren't read yet.",
  "Telemetry exported through the OpenTelemetry Collector's Azure Monitor exporter can carry span names instead of addresses as targets (gRPC, Redis, DNS). When the request join names the callee of every such call the span name is dropped; calls to services without telemetry of their own (a cache, a database) stay unresolved.",
  "Code from Azure DevOps and telemetry from Azure Monitor only; GitHub, GitLab and other telemetry backends need adapters.",
  "Messaging is modeled as service to broker, not producer to queue or topic to consumer.",
  "Applications outside Kubernetes, executing deployments and database migrations are out of scope.",
];

function Flows() {
  const box = "rounded-xl bg-white p-3 ring-1 ring-slate-200";
  const store = "rounded-xl bg-brand-50 p-3 ring-1 ring-brand-200";
  const arrow = <span aria-hidden="true" className="self-center text-center text-slate-400">→</span>;
  return (
    <div className="space-y-6">
      <div>
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Discovery (onboarding)</p>
        <div className="mt-2 grid gap-3 md:grid-cols-[1fr_auto_1fr_auto_1fr]">
          <div className={box}>
            <p className="font-semibold text-slate-900">Code, cluster, telemetry</p>
            <p className="text-sm">Three read-only scans</p>
          </div>
          {arrow}
          <div className={box}>
            <p className="font-semibold text-slate-900">Reconcile</p>
            <p className="text-sm">One identity per service; references resolved</p>
          </div>
          {arrow}
          <div className={store}>
            <p className="font-semibold text-slate-900">Baseline snapshot</p>
            <p className="text-sm">Immutable; written only if every source succeeded</p>
          </div>
        </div>
      </div>
      <div>
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Assessment (each pull request)</p>
        <div className="mt-2 grid gap-3 md:grid-cols-[1fr_auto_1fr_auto_1fr]">
          <div className={box}>
            <p className="font-semibold text-slate-900">Pull request</p>
            <p className="text-sm">Read by number from Azure DevOps</p>
          </div>
          {arrow}
          <div className={box}>
            <p className="font-semibold text-slate-900">Ten checks</p>
            <p className="text-sm">Against the pinned snapshot and live telemetry</p>
          </div>
          {arrow}
          <div className={store}>
            <p className="font-semibold text-slate-900">Briefing, then decision</p>
            <p className="text-sm">A named person signs; the record is hash-chained</p>
          </div>
        </div>
      </div>
    </div>
  );
}

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

            <Section id="flows" title="The two flows">
              <p>
                <strong className="text-slate-900">Discovery</strong> reads code, the cluster and telemetry,
                merges them into one dependency graph and keeps it as an immutable baseline snapshot.{" "}
                <strong className="text-slate-900">Assessment</strong> takes one pull request, works out what it
                touches against a pinned snapshot, checks every piece of evidence and produces a briefing. A
                person then records the decision.
              </p>
              <Flows />
            </Section>

            <Section id="search" title="How each source is searched">
              <p>
                Each source is read on its own and records what it could not read as carefully as what it
                found. A gap is reported, never skipped.
              </p>
              <div className="grid gap-4 lg:grid-cols-3">
                {SOURCES.map((source) => (
                  <article key={source.name} className="min-w-0 rounded-xl bg-white p-4 ring-1 ring-slate-200">
                    <div className="flex items-center gap-2">
                      <span className={`h-2.5 w-2.5 rounded-full ${source.color}`} aria-hidden="true" />
                      <h3 className="font-semibold text-slate-900">{source.name}</h3>
                    </div>
                    <p className="mt-1 text-xs text-slate-500">{source.where}</p>
                    <ul className="mt-3 space-y-2 text-sm">
                      {source.reads.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                    <p className="mt-3 border-t border-slate-100 pt-3 text-xs text-slate-500">{source.flags}</p>
                  </article>
                ))}
              </div>
            </Section>

            <Section id="merge" title="Merging into one graph">
              <p>
                A code service and a running service are linked only when their names are equal after
                ignoring case and <code>-_.</code>, and the match is unique on both sides. Near names are
                offered as questions, never applied. A variable read in code is resolved in this order:
              </p>
              <ol className="space-y-2 text-sm">
                {RESOLUTION.map(([title, body], index) => (
                  <li key={title} className="flex gap-3 rounded-lg bg-white p-3 ring-1 ring-slate-200">
                    <span className="font-mono text-xs font-semibold text-brand-600">{index + 1}</span>
                    <span>
                      <strong className="text-slate-900">{title}:</strong> {body}
                    </span>
                  </li>
                ))}
              </ol>
              <p>
                Dependencies seen by several sources are merged, keeping every source and the newest sighting.
                Anything that can&apos;t be pinned to exactly one service is kept as an unresolved endpoint (an
                IP address, an external or private host name, an unknown service name, a variable with an
                unknown or unset value, or an ambiguous name) for a person to judge.
              </p>
            </Section>

            <Section id="storage" title="What is stored where">
              <p>
                Everything lives under the server&apos;s data directory (<code>RC_DATA_DIR</code>). The platform
                deletes nothing; retention is the storage&apos;s backup. Secret values are never read, and only the
                server part of a connection string is kept.
              </p>
              <div className="overflow-x-auto rounded-xl ring-1 ring-slate-200">
                <table className="w-full min-w-[640px] text-left text-sm">
                  <thead className="bg-slate-50 text-slate-500">
                    <tr>
                      <th scope="col" className="px-4 py-3 font-medium">Record</th>
                      <th scope="col" className="px-4 py-3 font-medium">Contents</th>
                      <th scope="col" className="px-4 py-3 font-medium">Can it change?</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-200 bg-white">
                    {STORES.map(([name, path, contents, change]) => (
                      <tr key={name}>
                        <td className="px-4 py-3 align-top">
                          <p className="font-semibold text-slate-900">{name}</p>
                          <p className="font-mono text-xs text-slate-500 [overflow-wrap:anywhere]">{path}</p>
                        </td>
                        <td className="px-4 py-3 align-top text-slate-600">{contents}</td>
                        <td className="px-4 py-3 align-top text-slate-600">{change}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p>
                Each assessment&apos;s record downloads as one sealed JSON file, or opens as a print view, from the
                Assessments page.
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
                {DECISION_STEPS.map(([title, body, codes], index) => (
                  <li key={title} className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
                    <p className="font-mono text-xs font-semibold text-brand-600">
                      {String(index + 1).padStart(2, "0")}
                    </p>
                    <p className="mt-1 font-semibold text-slate-900">{title}</p>
                    <p className="mt-1 text-sm text-slate-600">{body}</p>
                    {codes && <p className="mt-2 font-mono text-[11px] text-slate-400 [overflow-wrap:anywhere]">{codes}</p>}
                  </li>
                ))}
              </ol>
            </Section>

            <Section id="reasons" title="Reason codes">
              <p>
                A verdict always carries its reasons. <strong className="text-slate-900">Approved</strong> means the
                advisory review approved and no reason applies. <strong className="text-slate-900">Actionable</strong>{" "}
                means the evidence is complete enough to rely on the verdict, even when it is No-go: only the last
                three codes leave a verdict actionable.
              </p>
              <div className="overflow-x-auto rounded-xl ring-1 ring-slate-200">
                <table className="w-full min-w-[560px] text-left text-sm">
                  <thead className="bg-slate-50 text-slate-500">
                    <tr>
                      <th scope="col" className="px-4 py-3 font-medium">Code</th>
                      <th scope="col" className="px-4 py-3 font-medium">Meaning</th>
                      <th scope="col" className="px-4 py-3 font-medium">Evidence complete?</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-200 bg-white">
                    {REASONS.map(([codes, meaning, blocks]) => (
                      <tr key={codes}>
                        <td className="px-4 py-3 align-top font-mono text-xs text-slate-800 [overflow-wrap:anywhere]">
                          {codes}
                        </td>
                        <td className="px-4 py-3 text-slate-600">{meaning}</td>
                        <td className="px-4 py-3 align-top">
                          <span
                            className={`whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${
                              blocks ? "bg-amber-50 text-amber-800" : "bg-emerald-50 text-emerald-800"
                            }`}
                          >
                            {blocks ? "No: not actionable" : "Yes: actionable"}
                          </span>
                        </td>
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
                    code changes. The policy scopes a shared workspace to the project, by
                    Application Insights component or by a cluster attribute such as{" "}
                    <code>k8s.cluster.name</code>.
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

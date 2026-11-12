import { useEffect, useRef, useState } from "react";
import ScopeInputCard from "./components/ScopeInputCard.jsx";

function TextField({ id, label, value, onChange, placeholder, hint }) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
      />
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

const INITIAL_CONFIG = {
  devopsOrgUrl: "",
  devopsProject: "",
  devopsRepos: "",
  k8sNamespaces: "",
  telemetryWorkspaceId: "",
};

const SYSTEMS = ["devops", "kubernetes", "telemetry"];

const IDLE_TEST = { status: "idle", message: "" };

const INITIAL_TESTS = Object.fromEntries(SYSTEMS.map((system) => [system, IDLE_TEST]));

// Which integration's access test is invalidated when a field changes.
const FIELD_SYSTEM = {
  devopsOrgUrl: "devops",
  devopsProject: "devops",
  devopsRepos: "devops",
  k8sNamespaces: "kubernetes",
  telemetryWorkspaceId: "telemetry",
};

const splitList = (value) =>
  value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);

const buildDevopsScope = (config) => ({
  devops_org_url: config.devopsOrgUrl.trim(),
  devops_project: config.devopsProject.trim(),
  target_repositories: splitList(config.devopsRepos),
});

// Only integrations whose access test succeeded contribute scope to the crawl;
// untested or failed form data is never sent.
const buildPayload = (config, tests) => ({
  ...(tests.devops.status === "success" ? buildDevopsScope(config) : {}),
  ...(tests.kubernetes.status === "success"
    ? { target_namespaces: splitList(config.k8sNamespaces) }
    : {}),
  ...(tests.telemetry.status === "success"
    ? { workspace_id: config.telemetryWorkspaceId.trim() }
    : {}),
});

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = Array.isArray(data?.detail)
      ? data.detail.map((item) => item.msg).join("; ")
      : data?.detail;
    throw new Error(detail || `Request failed with status ${response.status}`);
  }
  return data;
}

async function getJson(url) {
  const response = await fetch(url);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(data?.detail || `Request failed with status ${response.status}`);
  }
  return data;
}

const POLL_INTERVAL_MS = 2000;

const BANNER_STYLES = {
  info: "bg-indigo-50 text-indigo-800",
  success: "bg-green-50 text-green-800",
  error: "bg-red-50 text-red-700",
};

// Maps a polled job record onto the footer banner.
const jobBanner = (job) => {
  switch (job.status) {
    case "completed":
      if (job.results) {
        return { tone: "success", summary: job.results, text: "" };
      }
      return { tone: "success", text: `Discovery run completed. Job ID: ${job.job_id}` };
    case "failed":
      return {
        tone: "error",
        text: `Discovery run failed: ${job.error_message || "Unknown error"}. Job ID: ${job.job_id}`,
      };
    case "running":
      return { tone: "info", busy: true, text: `Discovery run in progress… Job ID: ${job.job_id}` };
    default:
      return { tone: "info", busy: true, text: `Discovery run queued. Job ID: ${job.job_id}` };
  }
};

const isTerminal = (status) => status === "completed" || status === "failed";

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent"
    />
  );
}

// Prominent warning that the active baseline relies on mock fixture data.
function DemoModeBanner() {
  return (
    <section
      role="alert"
      aria-labelledby="demo-mode-heading"
      className="rounded-md border-2 border-amber-500 bg-amber-100 px-4 py-3 text-amber-950"
    >
      <h2 id="demo-mode-heading" className="text-sm font-bold uppercase tracking-wide">
        ⚠ Demo Mode
      </h2>
      <p className="mt-1 text-sm">
        This baseline relies on mock telemetry fixtures, not observed traffic. Mock
        dependencies carry zero confidence and must not be used for real release
        decisions.
      </p>
    </section>
  );
}

// Shown in place of the status banner once a discovery run has completed.
function CrawlSummary({ results }) {
  return (
    <section
      role="status"
      aria-labelledby="crawl-summary-heading"
      className="rounded-md border border-green-200 bg-green-50 px-4 py-3 text-green-900"
    >
      <h2 id="crawl-summary-heading" className="text-sm font-semibold">
        Crawl Summary
      </h2>
      <p className="mt-1 text-sm">
        Baseline Established. Discovered {results.service_count} Services and{" "}
        {results.edge_count} Dependencies. The Release Coordinator is now active.
      </p>
      <p className="mt-1 font-mono text-xs text-green-800">
        Snapshot {results.tenant}/{results.project_id}/{results.snapshot_id}
      </p>
    </section>
  );
}

function LedgerSection({ title, count, children }) {
  return (
    <details className="group rounded-md border border-slate-200 bg-white">
      <summary className="cursor-pointer select-none px-3 py-2 text-sm font-medium text-slate-800">
        {title} ({count})
      </summary>
      <ul className="max-h-40 space-y-1 overflow-y-auto border-t border-slate-100 px-3 py-2 font-mono text-xs text-slate-700">
        {count === 0 ? <li className="font-sans text-slate-500">None discovered.</li> : children}
      </ul>
    </details>
  );
}

// Lists the extracted entities so users can verify the crawl, not just its counts.
function DiscoveryLedger({ services, edges }) {
  return (
    <section aria-labelledby="discovery-ledger-heading" className="space-y-2">
      <h2 id="discovery-ledger-heading" className="text-sm font-semibold text-slate-900">
        Discovery Ledger
      </h2>
      <LedgerSection title="Services" count={services.length}>
        {services.map((service) => (
          <li key={service.service_id}>{service.service_id}</li>
        ))}
      </LedgerSection>
      <LedgerSection title="Dependencies" count={edges.length}>
        {edges.map((edge, index) => (
          <li key={`${edge.source_service_id}->${edge.target_service_id}-${index}`}>
            {edge.source_service_id} ➔ {edge.target_service_id}
            {edge.provenance?.split(",").includes("mock_fixture") && (
              <span className="ml-2 rounded bg-amber-200 px-1 font-sans text-amber-900">
                mock
              </span>
            )}
          </li>
        ))}
      </LedgerSection>
    </section>
  );
}

export default function App() {
  const [config, setConfig] = useState(INITIAL_CONFIG);
  const [tests, setTests] = useState(INITIAL_TESTS);
  const [isCrawling, setIsCrawling] = useState(false);
  const [crawlMessage, setCrawlMessage] = useState(null);
  // Services and edges extracted by the last completed crawl, or null.
  const [ledger, setLedger] = useState(null);
  // Bumped whenever a system's scope changes or a new test starts, so a
  // response from an outdated request is discarded.
  const testGenerations = useRef(Object.fromEntries(SYSTEMS.map((system) => [system, 0])));
  const pollTimer = useRef(null);

  const stopPolling = () => {
    clearInterval(pollTimer.current);
    pollTimer.current = null;
  };

  useEffect(() => stopPolling, []);

  // While a job is being polled its banner stays visible.
  const clearCrawlMessage = () => {
    if (!pollTimer.current) setCrawlMessage(null);
  };

  const setTest = (system, testState) =>
    setTests((previous) => ({ ...previous, [system]: testState }));

  const update = (field) => (value) => {
    setConfig((previous) => ({ ...previous, [field]: value }));
    // A scope change invalidates only the affected integration's test.
    const system = FIELD_SYSTEM[field];
    testGenerations.current[system] += 1;
    setTest(system, IDLE_TEST);
    clearCrawlMessage();
  };

  const isVerified = SYSTEMS.some((system) => tests[system].status === "success");

  const runTest = async (system, request) => {
    const generation = ++testGenerations.current[system];
    setTest(system, { status: "loading", message: "" });
    clearCrawlMessage();
    let next;
    try {
      const result = await request();
      next = {
        status: result?.status === true ? "success" : "error",
        message: result?.message || "No response details returned.",
      };
    } catch (error) {
      next = { status: "error", message: error.message };
    }
    if (testGenerations.current[system] === generation) {
      setTest(system, next);
    }
  };

  const testDevops = () =>
    runTest("devops", () => {
      const scope = buildDevopsScope(config);
      if (!scope.devops_org_url || !scope.devops_project) {
        throw new Error("Enter the organization URL and project to test.");
      }
      return postJson("/api/verify/devops", scope);
    });

  const testKubernetes = () =>
    runTest("kubernetes", () => {
      const namespaces = splitList(config.k8sNamespaces);
      if (namespaces.length === 0) {
        throw new Error("Enter at least one namespace to test.");
      }
      return postJson("/api/verify/kubernetes", { target_namespaces: namespaces });
    });

  const testTelemetry = () =>
    runTest("telemetry", () => {
      const workspaceId = config.telemetryWorkspaceId.trim();
      if (!workspaceId) {
        throw new Error("Enter the Log Analytics workspace ID to test.");
      }
      return postJson("/api/verify/telemetry", { workspace_id: workspaceId });
    });

  const finishCrawl = (banner) => {
    stopPolling();
    setCrawlMessage(banner);
    setIsCrawling(false);
  };

  const pollJob = (jobId) => {
    let inFlight = false;
    pollTimer.current = setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const job = await getJson(`/api/onboard/${encodeURIComponent(jobId)}`);
        if (isTerminal(job.status)) {
          if (job.status === "completed" && job.results) {
            setLedger({
              services: job.results.services ?? [],
              edges: job.results.edges ?? [],
            });
          }
          finishCrawl(jobBanner(job));
        } else {
          setCrawlMessage(jobBanner(job));
        }
      } catch (error) {
        finishCrawl({
          tone: "error",
          text: `Could not read discovery run status: ${error.message}. Job ID: ${jobId}`,
        });
      } finally {
        inFlight = false;
      }
    }, POLL_INTERVAL_MS);
  };

  const startCrawl = async () => {
    if (!isVerified || isCrawling) return;
    stopPolling();
    setIsCrawling(true);
    setCrawlMessage(null);
    setLedger(null);
    try {
      const response = await postJson("/api/onboard", buildPayload(config, tests));
      setCrawlMessage(jobBanner({ job_id: response.job_id, status: "queued" }));
      pollJob(response.job_id);
    } catch (error) {
      finishCrawl({ tone: "error", text: error.message });
    }
  };

  return (
    <main className="mx-auto max-w-3xl px-4 pb-72 pt-10">
      <header className="mb-8">
        <h1 className="text-2xl font-bold text-slate-900">
          Release Coordinator Onboarding
        </h1>
        <p className="mt-2 text-sm text-slate-600">
          Every integration below is strictly read-only. Release Coordinator
          never creates, modifies, or deletes resources in your environment.
        </p>
      </header>

      <div className="space-y-6">
        <ScopeInputCard
          title="Azure DevOps"
          onTest={testDevops}
          testState={tests.devops}
          testDisabled={isCrawling}
          description="Repositories and pipeline definitions used to discover service configuration and endpoints."
          requiredRole="Project Reader (PAT/Entra scopes: Code (Read), Build (Read))"
          securityJustification="Only lists repositories, reads repository files, and reads build pipeline definitions and their variables. No write, queue, or administer permissions are requested."
        >
          <TextField
            id="devops-org-url"
            label="Organization URL"
            value={config.devopsOrgUrl}
            onChange={update("devopsOrgUrl")}
            placeholder="https://dev.azure.com/your-org"
          />
          <TextField
            id="devops-project"
            label="Project"
            value={config.devopsProject}
            onChange={update("devopsProject")}
            placeholder="your-project"
          />
          <TextField
            id="devops-repos"
            label="Repositories"
            value={config.devopsRepos}
            onChange={update("devopsRepos")}
            placeholder="payments-api, orders-service"
            hint="Comma-separated repository names."
          />
        </ScopeInputCard>

        <ScopeInputCard
          title="Kubernetes"
          onTest={testKubernetes}
          testState={tests.kubernetes}
          testDisabled={isCrawling}
          description="Services and workload manifests used to map the deployed topology and environment-driven dependencies of each namespace."
          requiredRole="Namespaced Role: get, list on services, configmaps, pods, deployments (apps), statefulsets (apps)"
          securityJustification="Bound per namespace via RoleBinding. Requires read-only access to Services, Deployments, StatefulSets, Pods, and ConfigMaps to resolve environment-driven dependencies. No access to Secrets or Pod exec, and no create, update, patch, or delete verbs."
        >
          <TextField
            id="k8s-namespaces"
            label="Namespaces"
            value={config.k8sNamespaces}
            onChange={update("k8sNamespaces")}
            placeholder="payments, orders"
            hint="Comma-separated namespace names."
          />
        </ScopeInputCard>

        <ScopeInputCard
          title="Telemetry"
          onTest={testTelemetry}
          testState={tests.telemetry}
          testDisabled={isCrawling}
          description="Application dependency telemetry used to confirm which service-to-service calls are actually active."
          requiredRole="Log Analytics Reader"
          securityJustification="Runs read-only KQL queries against the AppDependencies table for a bounded lookback window. Cannot modify workspace settings, data, or alerts."
        >
          <TextField
            id="telemetry-workspace-id"
            label="Log Analytics Workspace ID"
            value={config.telemetryWorkspaceId}
            onChange={update("telemetryWorkspaceId")}
            placeholder="00000000-0000-0000-0000-000000000000"
          />
        </ScopeInputCard>
      </div>

      <footer className="fixed inset-x-0 bottom-0 border-t border-slate-200 bg-white/95 shadow-lg backdrop-blur">
        <div className="mx-auto max-w-3xl space-y-3 px-4 py-4">
          <div aria-live="polite" className="max-h-48 space-y-2 overflow-y-auto">
            {crawlMessage?.summary?.demo_mode && <DemoModeBanner />}
            {crawlMessage?.summary && <CrawlSummary results={crawlMessage.summary} />}
            {crawlMessage?.summary && ledger && (
              <DiscoveryLedger services={ledger.services} edges={ledger.edges} />
            )}
            {crawlMessage && !crawlMessage.summary && (
              <p
                role={crawlMessage.tone === "error" ? "alert" : "status"}
                className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm ${
                  BANNER_STYLES[crawlMessage.tone]
                }`}
              >
                {crawlMessage.busy && <Spinner />}
                {crawlMessage.text}
              </p>
            )}
          </div>
          <div className="flex flex-col gap-3 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={startCrawl}
              disabled={!isVerified || isCrawling}
              title={
                isVerified ? undefined : "Run a successful access test on at least one integration first"
              }
              className="inline-flex items-center justify-center gap-2 rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isCrawling && <Spinner />}
              Start Day-Zero Crawl
            </button>
          </div>
        </div>
      </footer>
    </main>
  );
}

import { useRef, useState } from "react";
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

const buildPayload = (config) => ({
  target_repositories: splitList(config.devopsRepos),
  target_namespaces: splitList(config.k8sNamespaces),
  workspace_id: config.telemetryWorkspaceId.trim(),
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

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent"
    />
  );
}

export default function App() {
  const [config, setConfig] = useState(INITIAL_CONFIG);
  const [tests, setTests] = useState(INITIAL_TESTS);
  const [isCrawling, setIsCrawling] = useState(false);
  const [crawlMessage, setCrawlMessage] = useState(null);
  // Bumped whenever a system's scope changes or a new test starts, so a
  // response from an outdated request is discarded.
  const testGenerations = useRef(Object.fromEntries(SYSTEMS.map((system) => [system, 0])));

  const setTest = (system, testState) =>
    setTests((previous) => ({ ...previous, [system]: testState }));

  const update = (field) => (value) => {
    setConfig((previous) => ({ ...previous, [field]: value }));
    // A scope change invalidates only the affected integration's test.
    const system = FIELD_SYSTEM[field];
    testGenerations.current[system] += 1;
    setTest(system, IDLE_TEST);
    setCrawlMessage(null);
  };

  const isVerified = SYSTEMS.every((system) => tests[system].status === "success");

  const runTest = async (system, request) => {
    const generation = ++testGenerations.current[system];
    setTest(system, { status: "loading", message: "" });
    setCrawlMessage(null);
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

  const testDevops = () => runTest("devops", () => postJson("/api/verify/devops"));

  const testKubernetes = () =>
    runTest("kubernetes", () => {
      const namespaces = splitList(config.k8sNamespaces);
      if (namespaces.length === 0) {
        throw new Error("Enter at least one namespace to test.");
      }
      return postJson("/api/verify/kubernetes", { target_namespaces: namespaces });
    });

  const testTelemetry = () =>
    runTest("telemetry", () => postJson("/api/verify/telemetry"));

  const startCrawl = async () => {
    if (!isVerified) return;
    setIsCrawling(true);
    setCrawlMessage(null);
    try {
      const response = await postJson("/api/onboard", buildPayload(config));
      setCrawlMessage({ ok: true, text: `${response.message} Job ID: ${response.job_id}` });
    } catch (error) {
      setCrawlMessage({ ok: false, text: error.message });
    } finally {
      setIsCrawling(false);
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
          description="Service objects used to map the deployed topology of each namespace."
          requiredRole="Namespaced Role: get, list on services"
          securityJustification="Bound per namespace via RoleBinding. Only Service objects are listed. No access to Secrets, ConfigMaps, Pods/exec, or any create, update, patch, or delete verbs."
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
            {crawlMessage && (
              <p
                role={crawlMessage.ok ? "status" : "alert"}
                className={`rounded-md px-3 py-2 text-sm ${
                  crawlMessage.ok ? "bg-green-50 text-green-800" : "bg-red-50 text-red-700"
                }`}
              >
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
                isVerified ? undefined : "Run a successful access test on every integration first"
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

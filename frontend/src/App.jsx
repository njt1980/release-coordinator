import { useState } from "react";
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

const SYSTEM_LABELS = {
  devops: "Azure DevOps",
  kubernetes: "Kubernetes",
  telemetry: "Telemetry",
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
    body: JSON.stringify(body),
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

function VerificationResults({ results, error }) {
  if (error) {
    return (
      <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
        Connection test failed: {error}
      </p>
    );
  }
  if (!results) return null;
  return (
    <ul className="space-y-1">
      {Object.entries(SYSTEM_LABELS).map(([key, label]) => {
        const result = results[key];
        if (!result) return null;
        return (
          <li
            key={key}
            className={`rounded-md px-3 py-2 text-sm ${
              result.status ? "bg-green-50 text-green-800" : "bg-red-50 text-red-700"
            }`}
          >
            <span className="font-medium">
              {result.status ? "\u2713" : "\u2717"} {label}:
            </span>{" "}
            {result.message}
          </li>
        );
      })}
    </ul>
  );
}

export default function App() {
  const [config, setConfig] = useState(INITIAL_CONFIG);
  const [isVerifying, setIsVerifying] = useState(false);
  const [verifyResults, setVerifyResults] = useState(null);
  const [verifyError, setVerifyError] = useState(null);
  const [isVerified, setIsVerified] = useState(false);
  const [isCrawling, setIsCrawling] = useState(false);
  const [crawlMessage, setCrawlMessage] = useState(null);

  const update = (field) => (value) => {
    setConfig((previous) => ({ ...previous, [field]: value }));
    // Any scope change invalidates a previous successful verification.
    setIsVerified(false);
    setCrawlMessage(null);
  };

  const isBusy = isVerifying || isCrawling;

  const testConnections = async () => {
    setIsVerifying(true);
    setIsVerified(false);
    setVerifyResults(null);
    setVerifyError(null);
    setCrawlMessage(null);
    try {
      const results = await postJson("/api/verify", buildPayload(config));
      setVerifyResults(results);
      setIsVerified(
        Object.keys(SYSTEM_LABELS).every((key) => results?.[key]?.status === true)
      );
    } catch (error) {
      setVerifyError(error.message);
    } finally {
      setIsVerifying(false);
    }
  };

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
            {isVerifying && (
              <p className="flex items-center gap-2 text-sm text-slate-600">
                <Spinner /> Verifying read-only access to all integrations&hellip;
              </p>
            )}
            <VerificationResults results={verifyResults} error={verifyError} />
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
              onClick={testConnections}
              disabled={isBusy}
              className="inline-flex items-center justify-center gap-2 rounded-md border border-indigo-600 px-4 py-2 text-sm font-semibold text-indigo-600 hover:bg-indigo-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isVerifying && <Spinner />}
              Test Connections
            </button>
            <button
              type="button"
              onClick={startCrawl}
              disabled={!isVerified || isBusy}
              title={isVerified ? undefined : "Run a successful connection test first"}
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

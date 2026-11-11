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

export default function App() {
  const [config, setConfig] = useState(INITIAL_CONFIG);

  const update = (field) => (value) =>
    setConfig((previous) => ({ ...previous, [field]: value }));

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
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
    </main>
  );
}

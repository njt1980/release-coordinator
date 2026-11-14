// Release Coordinator showcase (resource group scope).
//
// Two passes (see deploy/showcase/README.md):
//   1. deployApp=false: identity, Key Vault, storage + share, Container Apps
//      environment, role assignments.
//   2. (secrets are put into Key Vault by the operator)
//   3. deployApp=true: the container app, scaled to zero when idle.
// Sign-in (Easy Auth) needs an Entra app registration (signinClientId) whose
// client secret is in Key Vault as 'signin-client-secret'; it is part of
// the template so a redeploy keeps it.

@description('Azure region.')
param location string = resourceGroup().location

@description('Short prefix for resource names.')
param prefix string = 'rcshow'

@description('Existing ACR holding the image (in acrResourceGroup).')
param acrName string

param acrResourceGroup string

@description('Existing Log Analytics workspace for the environment logs.')
param logWorkspaceName string

param logWorkspaceResourceGroup string

@description('Image tag in the ACR, e.g. release-coordinator:showcase-<sha>.')
param image string = 'release-coordinator:latest'

@description('Deploy the container app (pass 3).')
param deployApp bool = false

@description('Entra tenant and client ID of the read-only backend service principal.')
param backendTenantId string
param backendClientId string

@description('Client ID of the Entra app registration users sign in with (empty: no sign-in).')
param signinClientId string = ''

@description('Comma-separated Azure DevOps organization URLs the server may read.')
param adoOrgAllowlist string

@description('Entra app role (value) that may record release decisions; others can view and answer the review. Empty: anyone signed in may decide.')
param decisionRole string = 'Release.Coordinator'

param tags object = {
  project: 'release-coordinator'
  purpose: 'showcase'
}

var unique = uniqueString(resourceGroup().id)

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-id'
  location: location
  tags: tags
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${prefix}-kv-${take(unique, 8)}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

// Key Vault Secrets User
resource vaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identity.id, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: vault
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}

module acrPull 'acr-pull.bicep' = {
  name: 'acr-pull'
  scope: resourceGroup(acrResourceGroup)
  params: {
    acrName: acrName
    principalId: identity.properties.principalId
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: '${prefix}st${take(unique, 10)}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  name: '${storage.name}/default/rcdata'
  properties: {
    shareQuota: 5
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logWorkspaceName
  scope: resourceGroup(logWorkspaceResourceGroup)
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: 'rcdata'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: 'rcdata'
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [share]
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployApp) {
  name: '${prefix}-app'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        { server: '${acrName}.azurecr.io', identity: identity.id }
      ]
      secrets: [
        { name: 'anthropic-api-key', keyVaultUrl: '${vault.properties.vaultUri}secrets/anthropic-api-key', identity: identity.id }
        { name: 'backend-client-secret', keyVaultUrl: '${vault.properties.vaultUri}secrets/backend-client-secret', identity: identity.id }
        { name: 'microsoft-provider-authentication-secret', keyVaultUrl: '${vault.properties.vaultUri}secrets/signin-client-secret', identity: identity.id }
      ]
    }
    template: {
      containers: [
        {
          name: 'release-coordinator'
          image: '${acrName}.azurecr.io/${image}'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'RC_DATA_DIR', value: '/data' }
            // SMB refuses SQLite's byte-range locks; lock with a lock file.
            { name: 'RC_SQLITE_VFS', value: 'unix-dotfile' }
            { name: 'RC_TRUST_PRINCIPAL_HEADER', value: '1' }
            { name: 'RC_DECISION_ROLE', value: decisionRole }
            { name: 'RC_DISABLE_RAW_WEBHOOK', value: '1' }
            { name: 'RC_ADO_ORG_ALLOWLIST', value: adoOrgAllowlist }
            { name: 'TELEMETRY_SOURCE', value: 'azure' }
            { name: 'AZURE_TENANT_ID', value: backendTenantId }
            { name: 'AZURE_CLIENT_ID', value: backendClientId }
            { name: 'AZURE_CLIENT_SECRET', secretRef: 'backend-client-secret' }
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
          ]
          volumeMounts: [
            { volumeName: 'data', mountPath: '/data' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 }, periodSeconds: 30 }
            { type: 'Startup', httpGet: { path: '/healthz', port: 8000 }, periodSeconds: 5, failureThreshold: 24 }
          ]
        }
      ]
      // One replica at most: SQLite on the file share takes no concurrent writers.
      scale: { minReplicas: 0, maxReplicas: 1 }
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: environmentStorage.name
        }
      ]
    }
  }
  dependsOn: [vaultSecretsUser, acrPull]
}

resource auth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (deployApp && !empty(signinClientId)) {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: signinClientId
          clientSecretSettingName: 'microsoft-provider-authentication-secret'
          openIdIssuer: '${az.environment().authentication.loginEndpoint}${subscription().tenantId}/v2.0'
        }
      }
    }
  }
}

output vaultName string = vault.name
output storageName string = storage.name
output environmentName string = environment.name
output appName string = deployApp ? '${prefix}-app' : ''
output appFqdn string = app.?properties.configuration.ingress.fqdn ?? ''
output identityClientId string = identity.properties.clientId

// Azure AI Foundry account + project + model deployment for the Google Drive agent.
// Deployed into the Google-Drive resource group.
//
//   az deployment group create -g Google-Drive -f infra/foundry.bicep
//
// The new Foundry Agent Service ("new agents API": create_version /
// PromptAgentDefinition / conversations+responses) runs against the project
// endpoint that this template outputs.

@description('Azure region for the Foundry account.')
param location string = resourceGroup().location

@description('Base name; a unique suffix is appended for the global custom subdomain.')
param baseName string = 'gdrive-foundry'

@description('Project (child) name.')
param projectName string = 'gdrive-project'

@description('Chat model to deploy for the agent.')
param modelName string = 'gpt-4o-mini'

@description('Model version for the deployment.')
param modelVersion string = '2024-07-18'

@description('Deployment (TPM in thousands) capacity.')
param modelCapacity int = 50

var suffix = uniqueString(resourceGroup().id)
var accountName = '${baseName}-${suffix}'

// --- Azure AI Foundry account (Cognitive Services, kind AIServices) ----------
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Enables Foundry projects (the new agent service) on this account.
    allowProjectManagement: true
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

// --- Foundry project ---------------------------------------------------------
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
    description: 'Google Drive MCP agent project'
  }
}

// --- Model deployment (agent's chat model) -----------------------------------
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: modelName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
  }
  dependsOn: [
    project
  ]
}

output accountName string = account.name
output projectName string = project.name
output modelDeploymentName string = modelDeployment.name
// Project endpoint consumed by azure-ai-projects AIProjectClient(endpoint=...).
output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'

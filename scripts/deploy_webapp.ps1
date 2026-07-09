<#
.SYNOPSIS
  Deploy the self-hosted Google Drive MCP server (webapp/) to Azure App Service.

.DESCRIPTION
  Zips the CONTENTS of the webapp/ folder (so app.py sits at the zip root) and
  pushes it with `az webapp deploy`. Oryx installs webapp/requirements.txt during
  deployment (SCM_DO_BUILD_DURING_DEPLOYMENT=true).

  Run this after changing anything under webapp/. It does NOT touch app settings
  (Google credentials / shared secret) — use set_appservice_secrets.ps1 for those.

.EXAMPLE
  ./scripts/deploy_webapp.ps1
  ./scripts/deploy_webapp.ps1 -AppName gdrive-mcp-xxxx -ResourceGroup Google-Drive
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "Google-Drive",
    [string]$AppName       = "gdrive-mcp-dwavo67s",
    [string]$Subscription  = ""
)

$ErrorActionPreference = "Stop"

$repoRoot  = Split-Path -Parent $PSScriptRoot
$webappDir = Join-Path $repoRoot "webapp"
if (-not (Test-Path $webappDir)) { throw "webapp/ folder not found at $webappDir" }

if ($Subscription) { az account set --subscription $Subscription | Out-Null }

$zipPath = Join-Path $repoRoot "webapp_deploy.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

Write-Host "Zipping webapp/ contents -> $zipPath" -ForegroundColor Cyan
# Compress the *contents* of webapp/, excluding local caches, so app.py is at the root.
$items = Get-ChildItem -Path $webappDir -Force |
    Where-Object { $_.Name -notin @("__pycache__", ".venv", "local.settings.json") }
Compress-Archive -Path $items.FullName -DestinationPath $zipPath -Force

Write-Host "Deploying to App Service '$AppName' (resource group '$ResourceGroup')..." -ForegroundColor Cyan
az webapp deploy --resource-group $ResourceGroup --name $AppName --src-path $zipPath --type zip | Out-Null
if ($LASTEXITCODE -ne 0) { throw "az webapp deploy failed (exit $LASTEXITCODE)." }

Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

$base = "https://$AppName.azurewebsites.net"
Write-Host ""
Write-Host "Deployed. Probing health endpoint (build can take ~1-2 min the first time)..." -ForegroundColor Green
Start-Sleep -Seconds 5
try {
    $health = Invoke-RestMethod -Uri "$base/" -TimeoutSec 30
    Write-Host "  $base/ -> $($health | ConvertTo-Json -Compress)"
} catch {
    Write-Host "  Health probe not ready yet: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "  Retry in a minute:  Invoke-RestMethod $base/"
}

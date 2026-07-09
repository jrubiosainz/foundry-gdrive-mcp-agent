<#
.SYNOPSIS
  Push your Google OAuth credentials to the Azure App Service MCP server.

.DESCRIPTION
  Reads your LOCAL credentials.json (Web OAuth client) and token.json (minted by
  `python main.py auth`) and stores the client id/secret + refresh token as App
  Service application settings:

      GOOGLE_CLIENT_ID
      GOOGLE_CLIENT_SECRET
      GOOGLE_REFRESH_TOKEN

  The secrets go straight from your disk to Azure — they are never printed.
  The MCP server uses the refresh token to call the Google Drive API on your
  behalf, so the Foundry agent works from the portal with no per-user OAuth.

.NOTES
  Requires the Azure CLI (`az login`) with access to the subscription that owns
  the App Service. Run this AFTER `python main.py auth` has created token.json.

.EXAMPLE
  ./scripts/set_appservice_secrets.ps1
  ./scripts/set_appservice_secrets.ps1 -AppName gdrive-mcp-xxxx -ResourceGroup Google-Drive
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "Google-Drive",
    [string]$AppName       = "gdrive-mcp-dwavo67s",
    [string]$Subscription  = "",
    [string]$CredentialsFile = "credentials.json",
    [string]$TokenFile       = "token.json"
)

$ErrorActionPreference = "Stop"

function Read-JsonFile($path) {
    if (-not (Test-Path $path)) {
        throw "File not found: '$path'. Run this from the repo root (where $path lives)."
    }
    return Get-Content -Raw -Path $path | ConvertFrom-Json
}

Write-Host "Reading Google credentials from local files..." -ForegroundColor Cyan
$cred  = Read-JsonFile $CredentialsFile
$token = Read-JsonFile $TokenFile

# credentials.json for a Web client nests everything under "web".
$web = if ($cred.PSObject.Properties.Name -contains "web") { $cred.web } else { $cred.installed }
if (-not $web) { throw "credentials.json has neither a 'web' nor 'installed' section." }

$clientId     = $web.client_id
$clientSecret = $web.client_secret
$refreshToken = $token.refresh_token

if ([string]::IsNullOrWhiteSpace($clientId))     { throw "client_id missing from $CredentialsFile" }
if ([string]::IsNullOrWhiteSpace($clientSecret)) { throw "client_secret missing from $CredentialsFile" }
if ([string]::IsNullOrWhiteSpace($refreshToken)) {
    throw "refresh_token missing from $TokenFile. Delete token.json and re-run 'python main.py auth' (it must request offline access)."
}

if ($Subscription) {
    Write-Host "Setting subscription $Subscription ..." -ForegroundColor Cyan
    az account set --subscription $Subscription | Out-Null
}

Write-Host "Pushing settings to App Service '$AppName' (resource group '$ResourceGroup')..." -ForegroundColor Cyan
# Pass settings as an argument array so secret values are never interpolated into a command string / logs.
$settings = @(
    "GOOGLE_CLIENT_ID=$clientId",
    "GOOGLE_CLIENT_SECRET=$clientSecret",
    "GOOGLE_REFRESH_TOKEN=$refreshToken"
)
az webapp config appsettings set --resource-group $ResourceGroup --name $AppName --settings $settings | Out-Null
if ($LASTEXITCODE -ne 0) { throw "az webapp config appsettings set failed (exit $LASTEXITCODE)." }

Write-Host ""
Write-Host "Done. Google credentials stored on the App Service." -ForegroundColor Green
Write-Host ("  client_id     : {0}..." -f $clientId.Substring(0, [Math]::Min(20, $clientId.Length)))
Write-Host "  client_secret : (set, hidden)"
Write-Host "  refresh_token : (set, hidden)"
Write-Host ""
Write-Host "The app will restart automatically. Test with:" -ForegroundColor Cyan
Write-Host "  python main.py ask `"que documentos tengo en drive?`""

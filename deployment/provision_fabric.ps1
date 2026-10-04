[CmdletBinding()]
param(
    [string]$WorkspaceName,
    [string]$LakehouseName,
    [string]$CapacityId,
    [string]$CapacityResourceId,
    [string]$GeneratedRoot = (Join-Path (Split-Path $PSScriptRoot -Parent) "generated")
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Import-DotEnv.ps1")
Import-DotEnv
$WorkspaceName = if ($WorkspaceName) { $WorkspaceName } else { Get-RequiredEnvironmentVariable "FABRIC_WORKSPACE_NAME" }
$LakehouseName = if ($LakehouseName) { $LakehouseName } else { Get-RequiredEnvironmentVariable "FABRIC_LAKEHOUSE_NAME" }
$CapacityId = if ($CapacityId) { $CapacityId } else { Get-RequiredEnvironmentVariable "FABRIC_CAPACITY_ID" }
$CapacityResourceId = if ($CapacityResourceId) { $CapacityResourceId } else { Get-RequiredEnvironmentVariable "FABRIC_CAPACITY_RESOURCE_ID" }

function Get-AzToken {
    param([Parameter(Mandatory)][string]$Resource)
    $token = az account get-access-token --resource $Resource --query accessToken -o tsv
    if (-not $token) {
        throw "Unable to acquire access token for $Resource."
    }
    return $token
}

function Invoke-FabricApi {
    param(
        [Parameter(Mandatory)][ValidateSet("Get", "Post", "Delete")][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [object]$Body
    )
    $headers = @{ Authorization = "Bearer $(Get-AzToken -Resource 'https://api.fabric.microsoft.com')" }
    $parameters = @{
        Uri = "https://api.fabric.microsoft.com/v1/$Path"
        Headers = $headers
        Method = $Method
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json"
        $parameters.Body = $Body | ConvertTo-Json -Depth 10
    }
    return Invoke-RestMethod @parameters
}

if (-not (Test-Path (Join-Path $GeneratedRoot "manifest.json"))) {
    throw "Generated assets were not found at $GeneratedRoot. Run generate_demo.py first."
}

$capacity = (Invoke-FabricApi -Method Get -Path "capacities").value | Where-Object id -eq $CapacityId
if ($capacity.state -ne "Active") {
    Write-Host "Resuming Fabric capacity..."
    az resource invoke-action --ids $CapacityResourceId --action resume --api-version 2023-11-01 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Fabric capacity resume request failed."
    }
    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 10
        $capacity = (Invoke-FabricApi -Method Get -Path "capacities").value | Where-Object id -eq $CapacityId
        Write-Host "Capacity state: $($capacity.state)"
    } until ($capacity.state -eq "Active" -or (Get-Date) -ge $deadline)
}
if ($capacity.state -ne "Active") {
    throw "Fabric capacity did not become active before the timeout."
}

$workspace = (Invoke-FabricApi -Method Get -Path "workspaces").value | Where-Object displayName -eq $WorkspaceName
if (-not $workspace) {
    $workspace = Invoke-FabricApi -Method Post -Path "workspaces" -Body @{
        displayName = $WorkspaceName
        description = "Facilities work management, reliability, and technical-document data for synthetic OGE operations scenarios."
        capacityId = $CapacityId
    }
}
Write-Host "Workspace: $($workspace.displayName) [$($workspace.id)]"

$lakehouse = (Invoke-FabricApi -Method Get -Path "workspaces/$($workspace.id)/lakehouses").value | Where-Object displayName -eq $LakehouseName
if (-not $lakehouse) {
    $lakehouse = Invoke-FabricApi -Method Post -Path "workspaces/$($workspace.id)/lakehouses" -Body @{
        displayName = $LakehouseName
        description = "Maximo-style synthetic maintenance data and controlled operations documents."
    }
}
Write-Host "Lakehouse: $($lakehouse.displayName) [$($lakehouse.id)]"
$lakehouseDetails = Invoke-FabricApi -Method Get -Path "workspaces/$($workspace.id)/lakehouses/$($lakehouse.id)"
$sqlEndpointId = $lakehouseDetails.properties.sqlEndpointProperties.id
if (-not $sqlEndpointId) {
    throw "The lakehouse was provisioned, but its SQL analytics endpoint ID was unavailable."
}

$result = @{
    workspaceId = $workspace.id
    workspaceName = $workspace.displayName
    lakehouseId = $lakehouse.id
    lakehouseName = $lakehouse.displayName
    sqlEndpointId = $sqlEndpointId
    workspaceUrl = "https://app.fabric.microsoft.com/groups/$($workspace.id)"
}
$result | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $GeneratedRoot "fabric-deployment.json")
$result | ConvertTo-Json -Depth 5

[CmdletBinding()]
param(
    [string]$WorkspaceId,
    [string]$LakehouseId,
    [string]$SqlEndpointId,
    [string]$ModelName,
    [string]$GeneratedRoot = (Join-Path (Split-Path $PSScriptRoot -Parent) "generated")
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Import-DotEnv.ps1")
Import-DotEnv
$WorkspaceId = if ($WorkspaceId) { $WorkspaceId } else { Get-RequiredEnvironmentVariable "FABRIC_WORKSPACE_ID" }
$LakehouseId = if ($LakehouseId) { $LakehouseId } else { Get-RequiredEnvironmentVariable "FABRIC_LAKEHOUSE_ID" }
$SqlEndpointId = if ($SqlEndpointId) { $SqlEndpointId } else { Get-RequiredEnvironmentVariable "FABRIC_SQL_ENDPOINT_ID" }
$ModelName = if ($ModelName) { $ModelName } else { Get-RequiredEnvironmentVariable "FABRIC_SEMANTIC_MODEL_NAME" }

function Get-AzToken {
    param([Parameter(Mandatory)][string]$Resource)
    $token = az account get-access-token --resource $Resource --query accessToken -o tsv
    if (-not $token) { throw "Unable to acquire access token for $Resource." }
    return $token
}

function Invoke-FabricApi {
    param(
        [Parameter(Mandatory)][ValidateSet("Get", "Post")][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [object]$Body
    )
    $parameters = @{
        Uri = "https://api.fabric.microsoft.com/v1/$Path"
        Headers = @{ Authorization = "Bearer $(Get-AzToken -Resource 'https://api.fabric.microsoft.com')" }
        Method = $Method
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json"
        $parameters.Body = $Body | ConvertTo-Json -Depth 30
    }
    return Invoke-RestMethod @parameters
}

function Invoke-FabricLongRunningPost {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][object]$Body
    )
    $headers = @{ Authorization = "Bearer $(Get-AzToken -Resource 'https://api.fabric.microsoft.com')" }
    $response = Invoke-WebRequest -Uri "https://api.fabric.microsoft.com/v1/$Path" -Headers $headers -Method Post -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 30) -SkipHttpErrorCheck
    if ($response.StatusCode -ge 400) {
        throw "Fabric request failed with status $($response.StatusCode): $($response.Content)"
    }
    if ($response.StatusCode -ne 202) {
        return
    }
    $operationUri = $response.Headers.Location | Select-Object -First 1
    if (-not $operationUri) {
        throw "Fabric accepted the request without returning an operation URI."
    }
    do {
        Start-Sleep -Seconds 5
        $operation = Invoke-RestMethod -Uri $operationUri -Headers $headers
    } while ($operation.status -notin @("Succeeded", "Failed"))
    if ($operation.status -eq "Failed") {
        throw "Fabric operation failed: $($operation.error | ConvertTo-Json -Depth 10 -Compress)"
    }
}

function Ensure-OneLakeDirectory {
    param([string]$BaseUri, [string]$RelativePath, [hashtable]$Headers)
    $current = ""
    foreach ($segment in $RelativePath.Split("/", [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = if ($current) { "$current/$segment" } else { $segment }
        try {
            Invoke-WebRequest -Uri "$BaseUri/$current`?resource=directory" -Method Put -Headers $Headers | Out-Null
        }
        catch {
            if ($_.Exception.Response.StatusCode.value__ -ne 409) { throw }
        }
    }
}

function Send-OneLakeFile {
    param([string]$BaseUri, [string]$RelativePath, [string]$LocalPath, [hashtable]$Headers)
    $normalized = $RelativePath.Replace("\", "/")
    $parent = $normalized.Substring(0, $normalized.LastIndexOf("/"))
    Ensure-OneLakeDirectory -BaseUri $BaseUri -RelativePath $parent -Headers $Headers
    $encoded = ($normalized.Split("/") | ForEach-Object { [Uri]::EscapeDataString($_) }) -join "/"
    $uri = "$BaseUri/$encoded"
    Invoke-WebRequest -Uri "$uri`?resource=file" -Method Put -Headers $Headers | Out-Null
    $bytes = [IO.File]::ReadAllBytes($LocalPath)
    if ($bytes.Length -gt 0) {
        Invoke-WebRequest -Uri "$uri`?action=append&position=0" -Method Patch -Headers $Headers -ContentType "application/octet-stream" -Body $bytes | Out-Null
    }
    Invoke-WebRequest -Uri "$uri`?action=flush&position=$($bytes.Length)" -Method Patch -Headers $Headers | Out-Null
}

$tablesRoot = Join-Path $GeneratedRoot "tables"
$modelRoot = Join-Path $GeneratedRoot "semantic-model"
if (-not (Test-Path $tablesRoot) -or -not (Test-Path $modelRoot)) {
    throw "Generated tables or semantic model definition not found. Run materialize_tables_and_model.py first."
}

$storageHeaders = @{
    Authorization = "Bearer $(Get-AzToken -Resource 'https://storage.azure.com/')"
    "x-ms-version" = "2023-11-03"
}
$tablesUri = "https://onelake.dfs.fabric.microsoft.com/$WorkspaceId/$LakehouseId/Tables"
$uploadedFiles = 0
Get-ChildItem -Path $tablesRoot -Directory | ForEach-Object {
    $tableName = $_.Name
    $tableRoot = $_.FullName
    try {
        Invoke-WebRequest -Uri "$tablesUri/$tableName`?recursive=true" -Method Delete -Headers $storageHeaders | Out-Null
    }
    catch {
        if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
    }
    Get-ChildItem -Path $tableRoot -File -Recurse | ForEach-Object {
        $relative = "$tableName/" + [IO.Path]::GetRelativePath($tableRoot, $_.FullName).Replace("\", "/")
        Send-OneLakeFile -BaseUri $tablesUri -RelativePath $relative -LocalPath $_.FullName -Headers $storageHeaders
        $uploadedFiles++
    }
}

Write-Host "Uploaded $uploadedFiles Delta files across $((Get-ChildItem $tablesRoot -Directory).Count) managed table folders."
Start-Sleep -Seconds 30
Invoke-FabricLongRunningPost -Path "workspaces/$WorkspaceId/sqlEndpoints/$SqlEndpointId/refreshMetadata" -Body @{}

$parts = Get-ChildItem -Path $modelRoot -File -Recurse | ForEach-Object {
    @{
        path = [IO.Path]::GetRelativePath($modelRoot, $_.FullName).Replace("\", "/")
        payload = [Convert]::ToBase64String([IO.File]::ReadAllBytes($_.FullName))
        payloadType = "InlineBase64"
    }
}
$definition = @{ parts = @($parts) }
$existing = (Invoke-FabricApi -Method Get -Path "workspaces/$WorkspaceId/items").value |
    Where-Object { $_.type -eq "SemanticModel" -and $_.displayName -eq $ModelName }
if ($existing) {
    Invoke-FabricLongRunningPost -Path "workspaces/$WorkspaceId/semanticModels/$($existing.id)/updateDefinition?updateMetadata=true" -Body @{ definition = $definition }
}
else {
    Invoke-FabricLongRunningPost -Path "workspaces/$WorkspaceId/semanticModels" -Body @{
        displayName = $ModelName
        description = "Direct Lake model for Maximo-style facilities maintenance, asset reliability, labor, materials, preventive maintenance, and maintenance cost analysis."
        definition = $definition
    }
}
$model = (Invoke-FabricApi -Method Get -Path "workspaces/$WorkspaceId/items").value |
    Where-Object { $_.type -eq "SemanticModel" -and $_.displayName -eq $ModelName }
if (-not $model) {
    throw "The semantic model operation succeeded, but the model could not be found in the workspace."
}

$result = @{
    workspaceId = $WorkspaceId
    lakehouseId = $LakehouseId
    semanticModelId = $model.id
    semanticModelName = $ModelName
    tableCount = (Get-ChildItem $tablesRoot -Directory).Count
    deltaFileCount = $uploadedFiles
    modelUrl = "https://app.fabric.microsoft.com/groups/$WorkspaceId/datasets/$($model.id)"
}
$result | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $GeneratedRoot "table-model-deployment.json")
$result | ConvertTo-Json -Depth 5

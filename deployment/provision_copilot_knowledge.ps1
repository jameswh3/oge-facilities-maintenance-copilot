[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "Import-DotEnv.ps1")

Import-DotEnv -Path (Join-Path $root ".env")

$environmentUrl = Get-RequiredEnvironmentVariable "COPILOT_STUDIO_DATAVERSE_ENDPOINT"
$agentId = Get-RequiredEnvironmentVariable "COPILOT_STUDIO_AGENT_ID"
$sharePointPath = Join-Path $root "generated\sharepoint-deployment.json"
$settingsPath = Join-Path $root "copilot-studio\settings.mcs.yml"

if (-not (Test-Path -LiteralPath $sharePointPath)) {
    throw "SharePoint deployment output was not found. Run provision_sharepoint.ps1 first."
}

$sharePoint = Get-Content -LiteralPath $sharePointPath -Raw | ConvertFrom-Json
$settings = Get-Content -LiteralPath $settingsPath -Raw
$schemaMatch = [regex]::Match($settings, "(?m)^schemaName:\s*(.+)$")
if (-not $schemaMatch.Success) {
    throw "schemaName was not found in copilot-studio/settings.mcs.yml."
}
$agentSchemaName = $schemaMatch.Groups[1].Value.Trim()
$enableWorkOrderRag = $env:ENABLE_WORK_ORDER_RAG -match "^(?i:1|true|yes|on)$"

$knowledgeSources = @(
    [ordered]@{
        name = "Standard Operating Procedures"
        schemaBase = "StandardOperatingProcedures"
        description = "Use for controlled safety and work-management procedures, including switching, lockout/tagout, confined-space entry, hot work, spill response, inspections, planning, execution, and closeout. Cite the procedure title, document number, and revision; direct users to approved authorization processes."
    },
    [ordered]@{
        name = "Equipment Technical Manuals"
        schemaBase = "EquipmentTechnicalManuals"
        description = "Use for equipment operation, inspection, testing, preventive-maintenance intervals, troubleshooting, and manufacturer-style technical guidance. Cite the manual title, document number, and revision; do not treat guidance as authorization for safety-critical work."
    }
)

$baseUrl = $environmentUrl.TrimEnd("/")
$token = az account get-access-token --resource $baseUrl --query accessToken -o tsv
if (-not $token) {
    throw "Azure CLI did not return a Dataverse access token."
}
$headers = @{
    Authorization = "Bearer $token"
    "Content-Type" = "application/json"
}

$filter = [uri]::EscapeDataString(
    "_parentbotid_value eq $agentId and componenttype eq 16"
)
$componentsUri = (
    "$baseUrl/api/data/v9.2/botcomponents?" +
    "`$select=botcomponentid,name,description,schemaname,data&`$filter=$filter"
)
$existing = (Invoke-RestMethod -Headers $headers -Uri $componentsUri).value
$results = @()

foreach ($source in $knowledgeSources) {
    if ($source.name -notin $sharePoint.libraries) {
        throw "SharePoint library '$($source.name)' was not provisioned."
    }

    $libraryPath = [uri]::EscapeDataString($source.name)
    $libraryUrl = "$($sharePoint.siteUrl.TrimEnd('/'))/$libraryPath"
    $data = @"
kind: KnowledgeSourceConfiguration
source:
  kind: SharePointKnowledgeSource
  siteUrl: $libraryUrl
"@
    $component = $existing | Where-Object { $_.name -eq $source.name } | Select-Object -First 1

    if ($component) {
        $body = @{
            name = $source.name
            description = $source.description
            data = $data.TrimEnd()
        } | ConvertTo-Json
        Invoke-RestMethod `
            -Method Patch `
            -Headers $headers `
            -Uri "$baseUrl/api/data/v9.2/botcomponents($($component.botcomponentid))" `
            -Body $body
        $componentId = $component.botcomponentid
        $operation = "updated"
    } else {
        $suffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
        $componentSchemaName = "$agentSchemaName.$($source.schemaBase)_$suffix"
        $body = @{
            name = $source.name
            description = $source.description
            schemaname = $componentSchemaName
            componenttype = 16
            data = $data.TrimEnd()
            statecode = 0
            statuscode = 1
            "parentbotid@odata.bind" = "/bots($agentId)"
        } | ConvertTo-Json
        Invoke-RestMethod `
            -Method Post `
            -Headers $headers `
            -Uri "$baseUrl/api/data/v9.2/botcomponents" `
            -Body $body
        $schemaFilter = [uri]::EscapeDataString(
            "schemaname eq '$componentSchemaName' and _parentbotid_value eq $agentId"
        )
        $createdUri = (
            "$baseUrl/api/data/v9.2/botcomponents?" +
            "`$select=botcomponentid&`$filter=$schemaFilter"
        )
        $created = (Invoke-RestMethod -Headers $headers -Uri $createdUri).value |
            Select-Object -First 1
        if (-not $created) {
            throw "Dataverse did not return the created knowledge component."
        }
        $componentId = $created.botcomponentid
        $operation = "created"
    }

    $results += [ordered]@{
        name = $source.name
        description = $source.description
        url = $libraryUrl
        componentId = $componentId
        operation = $operation
    }
}

$workOrderRag = $null
if ($enableWorkOrderRag) {
    $ragPath = Join-Path $root "generated\work-order-rag-deployment.json"
    if (-not (Test-Path -LiteralPath $ragPath)) {
        throw (
            "Work-order RAG is enabled, but its deployment output was not found. " +
            "Run provision_work_order_rag.py first."
        )
    }
    $rag = Get-Content -LiteralPath $ragPath -Raw | ConvertFrom-Json
    $indexName = if ($rag.indexName) { $rag.indexName } else { $env:AZURE_AI_SEARCH_INDEX }
    if (-not $indexName) {
        throw "The Azure AI Search index name was not found."
    }

    $connectionReferenceName = "$agentSchemaName.cr.shared_azureaisearch"
    $connectionFilter = [uri]::EscapeDataString(
        "connectionreferencelogicalname eq '$connectionReferenceName'"
    )
    $connectionUri = (
        "$baseUrl/api/data/v9.2/connectionreferences?" +
        "`$select=connectionreferenceid,connectionreferencelogicalname&" +
        "`$filter=$connectionFilter"
    )
    $connectionReference = (
        Invoke-RestMethod -Headers $headers -Uri $connectionUri
    ).value | Select-Object -First 1
    if (-not $connectionReference) {
        $connectionBody = @{
            connectionreferencedisplayname = $connectionReferenceName
            connectionreferencelogicalname = $connectionReferenceName
            connectorid = "/providers/Microsoft.PowerApps/apis/shared_azureaisearch"
            statecode = 0
            statuscode = 1
        } | ConvertTo-Json
        Invoke-RestMethod `
            -Method Post `
            -Headers $headers `
            -Uri "$baseUrl/api/data/v9.2/connectionreferences" `
            -Body $connectionBody
        $connectionReference = (
            Invoke-RestMethod -Headers $headers -Uri $connectionUri
        ).value | Select-Object -First 1
        if (-not $connectionReference) {
            throw "Dataverse did not return the Azure AI Search connection reference."
        }
        $connectionOperation = "created"
    } else {
        $connectionOperation = "existing"
    }

    $toolName = "Find analogous completed work orders"
    $toolDescription = (
        "Search the Azure AI Search index of completed synthetic work orders when " +
        "the user asks for similar, analogous, related, or previously resolved cases. " +
        "Use the user request only as Search Text. Always use indexName " +
        "'completed-work-orders', vectorizedSearchFields ['content_vector'], selectFields " +
        "['wonum','assetnum','asset_description','title','problem_code','cause_code'," +
        "'remedy_code','content','status'], filterCondition ""status eq 'COMP' or status " +
        "eq 'CLOSE'"", nearestNeighbors 5, and top 5. Return work-order number, asset, " +
        "title/description, problem/cause/remedy codes, relevant historical details from " +
        "content, and search relevance. Treat historical cases as evidence, not repair " +
        "instructions."
    )
    $toolData = @"
kind: ConnectorTool
authMode: Invoker
connectionReference: $connectionReferenceName
connectorId: /providers/Microsoft.PowerApps/apis/shared_azureaisearch
operationId: IntegratedVectorSearch
toolInputs:
  - name: indexName
    value:
      kind: ValueReference
      type: '{"type":"string"}'
      defaultValue: '"$indexName"'

  - name: integratedVectorSearchRequest.vectorizedSearchFields
    value:
      kind: ValueReference
      type: '{"type":"array"}'
      defaultValue: '["content_vector"]'

  - name: integratedVectorSearchRequest.selectFields
    value:
      kind: ValueReference
      type: '{"type":"array"}'
      defaultValue: '["wonum","assetnum","asset_description","title","problem_code","cause_code","remedy_code","content","status"]'

  - name: integratedVectorSearchRequest.filterCondition
    value:
      kind: ValueReference
      type: '{"type":"string"}'
      defaultValue: '"status eq ''COMP'' or status eq ''CLOSE''"'

  - name: integratedVectorSearchRequest.nearestNeighbors
    value:
      kind: ValueReference
      type: '{"type":"integer"}'
      defaultValue: 5

  - name: integratedVectorSearchRequest.top
    value:
      kind: ValueReference
      type: '{"type":"integer"}'
      defaultValue: 5
"@
    $toolFilter = [uri]::EscapeDataString(
        "_parentbotid_value eq $agentId and componenttype eq 9 and name eq '$toolName'"
    )
    $toolUri = (
        "$baseUrl/api/data/v9.2/botcomponents?" +
        "`$select=botcomponentid,name,description,schemaname,data&`$filter=$toolFilter"
    )
    $tool = (Invoke-RestMethod -Headers $headers -Uri $toolUri).value |
        Select-Object -First 1
    if ($tool) {
        $toolBody = @{
            name = $toolName
            description = $toolDescription
            data = $toolData.TrimEnd()
        } | ConvertTo-Json
        Invoke-RestMethod `
            -Method Patch `
            -Headers $headers `
            -Uri "$baseUrl/api/data/v9.2/botcomponents($($tool.botcomponentid))" `
            -Body $toolBody
        $toolId = $tool.botcomponentid
        $toolOperation = "updated"
    } else {
        $suffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
        $toolSchemaName = "$agentSchemaName.tool.Searchvectorswithnaturallanguage_$suffix"
        $toolBody = @{
            name = $toolName
            description = $toolDescription
            schemaname = $toolSchemaName
            componenttype = 9
            data = $toolData.TrimEnd()
            statecode = 0
            statuscode = 1
            "parentbotid@odata.bind" = "/bots($agentId)"
        } | ConvertTo-Json
        Invoke-RestMethod `
            -Method Post `
            -Headers $headers `
            -Uri "$baseUrl/api/data/v9.2/botcomponents" `
            -Body $toolBody
        $tool = (Invoke-RestMethod -Headers $headers -Uri $toolUri).value |
            Select-Object -First 1
        if (-not $tool) {
            throw "Dataverse did not return the Azure AI Search tool component."
        }
        $toolId = $tool.botcomponentid
        $toolOperation = "created"
    }
    $workOrderRag = [ordered]@{
        serviceEndpoint = $rag.endpoint
        indexName = $indexName
        toolName = $toolName
        toolComponentId = $toolId
        toolOperation = $toolOperation
        connectionReference = $connectionReferenceName
        connectionReferenceOperation = $connectionOperation
        authentication = "Invoker"
        connectionSetup = "Access Key stored in the Power Platform encrypted connection store"
    }
}

$published = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    $publishOutput = @(
        & pac copilot publish --bot $agentId --environment $baseUrl 2>&1
    )
    $publishText = $publishOutput -join [Environment]::NewLine
    if (
        $LASTEXITCODE -eq 0 -and
        $publishText -match "Published successfully!" -and
        $publishText -notmatch "(?i)non-recoverable error|Exception Type:|^Error:"
    ) {
        $publishOutput | ForEach-Object { Write-Host $_ }
        $published = $true
        break
    }
    if ($attempt -lt 3) {
        Start-Sleep -Seconds (5 * $attempt)
    }
}
if (-not $published) {
    throw "Copilot Studio publication failed after 3 attempts.`n$publishText"
}

$verification = (Invoke-RestMethod -Headers $headers -Uri $componentsUri).value
foreach ($result in $results) {
    $component = $verification |
        Where-Object { $_.botcomponentid -eq $result.componentId } |
        Select-Object -First 1
    if (-not $component) {
        throw "Knowledge component '$($result.name)' was not found after provisioning."
    }
    if ($component.description -ne $result.description) {
        throw "Knowledge component '$($result.name)' has an unexpected description."
    }
}
if ($workOrderRag) {
    $verifiedTool = (Invoke-RestMethod -Headers $headers -Uri $toolUri).value |
        Select-Object -First 1
    if (-not $verifiedTool) {
        throw "The Azure AI Search tool component was not found after provisioning."
    }
    if (
        $verifiedTool.description -ne $toolDescription -or
        $verifiedTool.data -notmatch [regex]::Escape($indexName)
    ) {
        throw "The Azure AI Search tool component has unexpected configuration."
    }
}

$output = [ordered]@{
    agentId = $agentId
    published = $published
    knowledgeSources = $results
    workOrderRag = $workOrderRag
}
$outputPath = Join-Path $root "generated\copilot-knowledge-deployment.json"
$output | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $outputPath
$output | ConvertTo-Json -Depth 6

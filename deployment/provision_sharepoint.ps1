[CmdletBinding()]
param(
    [string]$TenantId,
    [string]$TenantHost,
    [string]$SiteTitle,
    [string]$SiteAlias,
    [string]$GeneratedRoot = (Join-Path (Split-Path $PSScriptRoot -Parent) "generated")
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Import-DotEnv.ps1")
Import-DotEnv
$TenantId = if ($TenantId) { $TenantId } else { Get-RequiredEnvironmentVariable "MICROSOFT_TENANT_ID" }
$TenantHost = if ($TenantHost) { $TenantHost } else { Get-RequiredEnvironmentVariable "SHAREPOINT_TENANT_HOST" }
$SiteTitle = if ($SiteTitle) { $SiteTitle } else { Get-RequiredEnvironmentVariable "SHAREPOINT_SITE_TITLE" }
$SiteAlias = if ($SiteAlias) { $SiteAlias } else { Get-RequiredEnvironmentVariable "SHAREPOINT_SITE_ALIAS" }
$GraphResourceAppId = "00000003-0000-0000-c000-000000000000"
$ProvisioningAppName = "OGE Operations Knowledge Provisioning $([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"
$application = $null
$servicePrincipal = $null

function Get-UserGraphToken {
    $token = az account get-access-token --resource-type ms-graph --query accessToken -o tsv
    if (-not $token) { throw "Unable to acquire a delegated Microsoft Graph token." }
    return $token
}

function Invoke-Graph {
    param(
        [Parameter(Mandatory)][ValidateSet("Get", "Post", "Put", "Delete")][string]$Method,
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Token,
        [object]$Body,
        [string]$ContentType = "application/json"
    )
    $parameters = @{ Uri = $Uri; Headers = @{ Authorization = "Bearer $Token" }; Method = $Method }
    if ($null -ne $Body) {
        $parameters.ContentType = $ContentType
        $parameters.Body = if ($ContentType -eq "application/json") { $Body | ConvertTo-Json -Depth 20 } else { $Body }
    }
    return Invoke-RestMethod @parameters
}

function Wait-ForSite {
    param([string]$GroupId, [string]$Token)
    $deadline = (Get-Date).AddMinutes(10)
    do {
        try {
            return Invoke-Graph -Method Get -Uri "https://graph.microsoft.com/v1.0/groups/$GroupId/sites/root?`$select=id,displayName,webUrl" -Token $Token
        }
        catch {
            if ((Get-Date) -ge $deadline) { throw }
            Start-Sleep -Seconds 15
        }
    } while ($true)
}

function Wait-ForServicePrincipal {
    param([string]$AppId, [string]$Token)
    $deadline = (Get-Date).AddMinutes(5)
    do {
        $result = Invoke-Graph -Method Get -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId%20eq%20'$AppId'" -Token $Token
        if ($result.value.Count -gt 0) { return $result.value[0] }
        if ((Get-Date) -ge $deadline) { throw "Service principal creation timed out." }
        Start-Sleep -Seconds 5
    } while ($true)
}

function Send-DriveFile {
    param([string]$DriveId, [string]$LocalPath, [string]$Token)
    $name = [Uri]::EscapeDataString([IO.Path]::GetFileName($LocalPath))
    return Invoke-RestMethod `
        -Method Put `
        -Uri "https://graph.microsoft.com/v1.0/drives/$DriveId/root:/$name`:/content" `
        -Headers @{ Authorization = "Bearer $Token" } `
        -InFile $LocalPath `
        -ContentType "application/octet-stream"
}

function Get-AppGraphToken {
    param(
        [Parameter(Mandatory)][string]$ClientId,
        [Parameter(Mandatory)][string]$ClientSecret
    )
    $deadline = (Get-Date).AddMinutes(3)
    do {
        try {
            return (Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/$TenantId/oauth2/v2.0/token" -ContentType "application/x-www-form-urlencoded" -Body @{
                client_id = $ClientId
                client_secret = $ClientSecret
                scope = "https://graph.microsoft.com/.default"
                grant_type = "client_credentials"
            }).access_token
        }
        catch {
            if ((Get-Date) -ge $deadline) { throw }
            Start-Sleep -Seconds 10
        }
    } while ($true)
}

function Wait-ForSiteDrives {
    param(
        [Parameter(Mandatory)][string]$SiteId,
        [Parameter(Mandatory)][string]$Token
    )
    $deadline = (Get-Date).AddMinutes(5)
    do {
        try {
            return Invoke-Graph -Method Get -Uri "https://graph.microsoft.com/v1.0/sites/$SiteId/drives?`$select=id,name,webUrl" -Token $Token
        }
        catch {
            if ((Get-Date) -ge $deadline) { throw }
            Start-Sleep -Seconds 15
        }
    } while ($true)
}

if (-not (Test-Path (Join-Path $GeneratedRoot "manifest.json"))) {
    throw "Generated assets were not found at $GeneratedRoot. Run generate_demo.py first."
}

$userToken = Get-UserGraphToken
$me = Invoke-Graph -Method Get -Uri "https://graph.microsoft.com/v1.0/me?`$select=id,userPrincipalName" -Token $userToken
$escapedAlias = [Uri]::EscapeDataString($SiteAlias)
$existingGroups = Invoke-Graph -Method Get -Uri "https://graph.microsoft.com/v1.0/groups?`$filter=mailNickname%20eq%20'$escapedAlias'&`$select=id,displayName,mailNickname" -Token $userToken
if ($existingGroups.value.Count -gt 0) {
    $group = $existingGroups.value[0]
}
else {
    $group = Invoke-Graph -Method Post -Uri "https://graph.microsoft.com/v1.0/groups" -Token $userToken -Body @{
        displayName = $SiteTitle
        description = "Controlled standard operating procedures and equipment technical manuals supporting OGE facilities maintenance and reliability."
        groupTypes = @("Unified")
        mailEnabled = $true
        mailNickname = $SiteAlias
        securityEnabled = $false
        visibility = "Private"
        "owners@odata.bind" = @("https://graph.microsoft.com/v1.0/users/$($me.id)")
        "members@odata.bind" = @("https://graph.microsoft.com/v1.0/users/$($me.id)")
    }
}
$site = Wait-ForSite -GroupId $group.id -Token $userToken

try {
    $graphSp = (Invoke-Graph -Method Get -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId%20eq%20'$GraphResourceAppId'" -Token $userToken).value[0]
    $sitesRole = $graphSp.appRoles | Where-Object { $_.value -eq "Sites.FullControl.All" -and $_.allowedMemberTypes -contains "Application" }
    if (-not $sitesRole) { throw "Microsoft Graph Sites.FullControl.All application role was not found." }

    $application = Invoke-Graph -Method Post -Uri "https://graph.microsoft.com/v1.0/applications" -Token $userToken -Body @{
        displayName = $ProvisioningAppName
        signInAudience = "AzureADMyOrg"
        requiredResourceAccess = @(@{
            resourceAppId = $GraphResourceAppId
            resourceAccess = @(@{ id = $sitesRole.id; type = "Role" })
        })
    }
    $servicePrincipal = Invoke-Graph -Method Post -Uri "https://graph.microsoft.com/v1.0/servicePrincipals" -Token $userToken -Body @{ appId = $application.appId }
    $servicePrincipal = Wait-ForServicePrincipal -AppId $application.appId -Token $userToken
    Invoke-Graph -Method Post -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($servicePrincipal.id)/appRoleAssignments" -Token $userToken -Body @{
        principalId = $servicePrincipal.id
        resourceId = $graphSp.id
        appRoleId = $sitesRole.id
    } | Out-Null
    $secret = Invoke-Graph -Method Post -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)/addPassword" -Token $userToken -Body @{
        passwordCredential = @{
            displayName = "Ephemeral provisioning credential"
            endDateTime = [DateTime]::UtcNow.AddHours(2).ToString("o")
        }
    }
    $appToken = Get-AppGraphToken -ClientId $application.appId -ClientSecret $secret.secretText
    Start-Sleep -Seconds 10

    $libraries = @(
        @{ Name = "Standard Operating Procedures"; Description = "Approved operating, safety, environmental, and work-management procedures."; Folder = "Standard Operating Procedures" },
        @{ Name = "Equipment Technical Manuals"; Description = "Controlled technical manuals and maintenance guides for utility facility equipment."; Folder = "Equipment Technical Manuals" }
    )
    $drives = Wait-ForSiteDrives -SiteId $site.id -Token $appToken
    $uploaded = @()
    foreach ($library in $libraries) {
        $drive = $drives.value | Where-Object name -eq $library.Name
        if (-not $drive) {
            Invoke-Graph -Method Post -Uri "https://graph.microsoft.com/v1.0/sites/$($site.id)/lists" -Token $appToken -Body @{
                displayName = $library.Name
                description = $library.Description
                list = @{ template = "documentLibrary" }
            } | Out-Null
            $libraryDeadline = (Get-Date).AddMinutes(2)
            do {
                Start-Sleep -Seconds 5
                $drives = Wait-ForSiteDrives -SiteId $site.id -Token $appToken
                $drive = $drives.value | Where-Object name -eq $library.Name
            } until ($drive -or (Get-Date) -ge $libraryDeadline)
        }
        if (-not $drive) { throw "Document library '$($library.Name)' was not provisioned." }
        Get-ChildItem -Path (Join-Path (Join-Path $GeneratedRoot "documents") $library.Folder) -File | ForEach-Object {
            $item = Send-DriveFile -DriveId $drive.id -LocalPath $_.FullName -Token $appToken
            $uploaded += @{ library = $library.Name; name = $item.name; webUrl = $item.webUrl }
        }
    }

    $result = @{
        groupId = $group.id
        siteId = $site.id
        siteTitle = $SiteTitle
        siteUrl = $site.webUrl
        libraries = $libraries.Name
        uploadedFiles = $uploaded.Count
        files = $uploaded
    }
    $result | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $GeneratedRoot "sharepoint-deployment.json")
    $result | ConvertTo-Json -Depth 10
}
finally {
    if ($application) {
        try {
            Invoke-Graph -Method Delete -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" -Token $userToken | Out-Null
            Write-Host "Removed temporary provisioning application '$ProvisioningAppName'."
        }
        catch {
            Write-Warning "Failed to remove temporary provisioning application '$ProvisioningAppName': $($_.Exception.Message)"
            throw
        }
    }
}

function Import-DotEnv {
    [CmdletBinding()]
    param(
        [string]$Path = (Join-Path (Split-Path $PSScriptRoot -Parent) ".env")
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Environment file not found at $Path. Copy .env.example to .env and configure it."
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) {
            throw "Invalid .env entry: $line"
        }

        $name = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1).Trim()
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Get-RequiredEnvironmentVariable {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Required environment variable $Name is not set."
    }
    return $value
}

# Contributing

Contributions that improve portability, reliability, documentation, or synthetic
demo coverage are welcome.

## Before opening a pull request

1. Create a focused branch from `main`.
2. Keep all example data synthetic. Do not add customer, employee, operational, or
   other confidential data.
3. Never commit `.env`, generated deployment output, credentials, tenant identifiers,
   connection metadata, or access tokens.
4. Preserve the approved source routing:
   - Fabric Data Agent for governed analytics.
   - SharePoint for controlled procedures and technical manuals.
   - Azure AI Search for analogous completed work orders.
5. Update directly related documentation and diagrams when behavior changes.

## Local validation

Run these checks from the repository root:

```powershell
python -m compileall -q .\deployment

$failed = @()
Get-ChildItem .\deployment -Filter *.ps1 | ForEach-Object {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $_.FullName,
        [ref]$tokens,
        [ref]$errors
    ) | Out-Null
    if ($errors.Count) {
        $failed += "$($_.Name): $($errors.Message -join '; ')"
    }
}
if ($failed.Count) {
    throw ($failed -join [Environment]::NewLine)
}
```

Also confirm that:

- `deploy_solution.ipynb` is valid JSON and has no saved execution output.
- the draw.io and SVG architecture files are valid XML.
- no generated or environment-specific files are staged.
- deployment changes are tested against the smallest relevant live or mocked surface.

## Pull requests

Describe the problem, the implementation, and the validation performed. Explicitly
call out architecture changes. Architecture changes must be discussed and approved
before implementation.

## Security reports

Do not disclose vulnerabilities or credentials in a public issue. Follow
[SECURITY.md](SECURITY.md) instead.

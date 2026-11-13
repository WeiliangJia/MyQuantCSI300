param(
    [int]$BatchSize = 100,
    [int]$UniverseSize = 941,
    [int]$StartOffset = 0,
    [string]$StartDate = "2005-07-01",
    [string]$EndDate = "2026-07-17",
    [double]$RequestSpacingSeconds = 1.0
)

$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

for ($offset = $StartOffset; $offset -lt $UniverseSize; $offset += $BatchSize) {
    Write-Output "[batch] offset=$offset limit=$BatchSize started=$(Get-Date -Format o)"
    python -m myquant.cli backfill-csi300-circ-mv $StartDate $EndDate `
        --offset $offset --limit $BatchSize `
        --request-spacing-seconds $RequestSpacingSeconds
    Write-Output "[batch] offset=$offset finished=$(Get-Date -Format o) exit_code=$LASTEXITCODE"
}

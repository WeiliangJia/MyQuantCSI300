param(
    [int]$BatchSize = 100,
    [int]$UniverseSize = 941,
    [string]$StartDate = "2005-07-01",
    [string]$EndDate = "2026-07-17"
)

$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

for ($offset = 0; $offset -lt $UniverseSize; $offset += $BatchSize) {
    Write-Output "[batch] offset=$offset limit=$BatchSize started=$(Get-Date -Format o)"
    python -m myquant.cli backfill-csi300-baostock $StartDate $EndDate --offset $offset --limit $BatchSize
    Write-Output "[batch] offset=$offset finished=$(Get-Date -Format o) exit_code=$LASTEXITCODE"
}

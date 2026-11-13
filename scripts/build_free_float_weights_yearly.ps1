param(
    [int]$StartYear = 2005,
    [int]$EndYear = 2026,
    [string]$FirstDate = "2005-07-01",
    [string]$LastDate = "2026-07-17"
)

$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

for ($year = $StartYear; $year -le $EndYear; $year++) {
    $startDate = if ($year -eq $StartYear) { $FirstDate } else { "$year-01-01" }
    $endDate = if ($year -eq $EndYear) { $LastDate } else { "$year-12-31" }
    Write-Output "[year] $year started=$(Get-Date -Format o) range=$startDate..$endDate"
    python -m myquant.cli build-free-float-weights $startDate $endDate
    Write-Output "[year] $year finished=$(Get-Date -Format o) exit_code=$LASTEXITCODE"
}

python -m myquant.cli data-audit

#Requires -Version 5.1
<#
.SYNOPSIS
  COPE v0 — Control-Oriented Privilege Engine (enumeration + scoring).

.DESCRIPTION
  Security research tool: maps Windows elevation / privilege surfaces and
  ranks them for remote desktop Control goals (High inject, silent reuse),
  not classic "shell to SYSTEM" bingo.

  Default mode: ENUMERATE ONLY. Never triggers UAC. Never runs exploits.

  -Verify is reserved for future lab checks (e.g. re-read elevated after
  starting an *already installed* Highest task). Not enabled in v0 beyond
  a dry-run flag acknowledgment.

.PARAMETER Out
  JSON report path (default: .\cope-report.json)

.PARAMETER Markdown
  Also write .md summary next to JSON

.PARAMETER Verify
  Lab-only (v0: only validates known Hogwarts silent task state; no elevating)

.PARAMETER Quiet
  Less console noise
#>
param(
    [string]$Out = "",
    [switch]$Markdown,
    [switch]$Verify,
    [switch]$Quiet
)

$ErrorActionPreference = "Continue"
$ScriptRoot = $PSScriptRoot
. (Join-Path $ScriptRoot "Cope.Core.ps1")
. (Join-Path $ScriptRoot "Cope.Modules.ps1")

if (-not $Out) {
    $Out = Join-Path (Get-Location) "cope-report.json"
}

$ctx = New-CopeContext -Verify:$Verify
$findings = @()

$modules = @(
    "token",
    "process",
    "uac_policy",
    "tasks",
    "services",
    "autoelevate",
    "hogwarts"
)

foreach ($m in $modules) {
    if (-not $Quiet) { Write-Host "[COPE] collect $m ..." }
    try {
        $fn = Get-Command "Invoke-CopeModule_$m" -ErrorAction Stop
        $part = & $fn -Context $ctx
        if ($part) { $findings += @($part) }
    } catch {
        $findings += New-CopeFinding -Id "err.$m" -Title "Module $m failed" -Category "error" `
            -Evidence @{ error = "$_" } -Consent "unknown" -Confidence 0.2 `
            -Goals @{ G1 = 0; G2 = 0; G3 = 0; G4 = 0; G5 = 0; G6 = 0 }
    }
}

$scored = $findings | ForEach-Object { Add-CopeScore -Finding $_ } | Sort-Object -Property score -Descending

$report = [ordered]@{
    engine      = "COPE"
    version     = "0.1.0"
    generated   = (Get-Date).ToUniversalTime().ToString("o")
    host        = $env:COMPUTERNAME
    user        = $env:USERNAME
    verify      = [bool]$Verify
    mode        = "enumerate"
    thesis      = "Control-oriented privilege surfaces (inject/capture), silent preferred"
    weights     = Get-CopeWeights
    findings    = @($scored)
    top         = @($scored | Select-Object -First 8 | ForEach-Object {
        [ordered]@{
            id    = $_.id
            title = $_.title
            score = $_.score
            consent = $_.consent
            goals = $_.goals
        }
    })
}

$dir = Split-Path -Parent $Out
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
($report | ConvertTo-Json -Depth 10) | Set-Content -Path $Out -Encoding UTF8
if (-not $Quiet) { Write-Host "[COPE] wrote $Out ($($scored.Count) findings)" }

if ($Markdown -or $true) {
    $mdPath = [IO.Path]::ChangeExtension($Out, ".md")
    $md = New-CopeMarkdown -Report $report
    $md | Set-Content -Path $mdPath -Encoding UTF8
    if (-not $Quiet) { Write-Host "[COPE] wrote $mdPath" }
}

# Console table
if (-not $Quiet) {
    Write-Host ""
    Write-Host "Top findings (Control score):"
    $scored | Select-Object -First 10 | ForEach-Object {
        "{0,6:N2}  {1,-12}  {2}" -f $_.score, $_.consent, $_.title
    }
}

# Optional verify: only inspect whether Hogwarts Highest task exists/running
if ($Verify) {
    if (-not $Quiet) { Write-Host "[COPE] verify: Hogwarts silent task state only (no elevation attempt)" }
    $t = Get-ScheduledTask -TaskName "HogwartsAgentElevated" -ErrorAction SilentlyContinue
    if ($t) {
        Write-Host "[COPE] verify: HogwartsAgentElevated State=$($t.State) RunLevel check via task definition"
    } else {
        Write-Host "[COPE] verify: HogwartsAgentElevated not installed — run install-elevated-task.ps1 once (admin)"
    }
}

exit 0

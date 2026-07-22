#Requires -Version 5.1
<#
.SYNOPSIS
  One-time install: Hogwarts agent as a Highest-privilege scheduled task.

.DESCRIPTION
  After this runs once (as Administrator), later starts use start-agent-silent.ps1
  / schtasks /Run and do NOT show a UAC Yes/No prompt.

  RunAs (Start-Process -Verb RunAs) always prompts — do not use that for daily start.

.PARAMETER AgentDir
  Directory containing the agent binary / python entry + agent.json

.PARAMETER TaskName
  Scheduled task name (default HogwartsAgentElevated)

.PARAMETER Python
  python.exe for lab agent (default: python on PATH). Ignored if -Exe is set.

.PARAMETER Exe
  Path to .NET agent exe if not using python lab agent.

.PARAMETER AtLogon
  Also start elevated agent at user logon (no prompt).
#>
param(
    [string]$AgentDir = "",
    [string]$TaskName = "HogwartsAgentElevated",
    [string]$Python = "python",
    [string]$Exe = "",
    [switch]$AtLogon
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
    Write-Host "This installer must run elevated ONCE (right-click PowerShell → Run as administrator)."
    Write-Host "After install, daily start is silent via start-agent-silent.ps1 (no UAC)."
    exit 1
}

if (-not $AgentDir) {
    $AgentDir = Split-Path -Parent $PSScriptRoot
    if (-not (Test-Path (Join-Path $AgentDir "agent.json")) -and (Test-Path (Join-Path $PSScriptRoot "..\agent.json"))) {
        $AgentDir = Resolve-Path (Join-Path $PSScriptRoot "..")
    }
}
$AgentDir = (Resolve-Path $AgentDir).Path

# Build action
if ($Exe -and (Test-Path $Exe)) {
    $exePath = (Resolve-Path $Exe).Path
    $arg = ""
    $work = Split-Path -Parent $exePath
    $action = New-ScheduledTaskAction -Execute $exePath -WorkingDirectory $work
} else {
    # Lab / python agent
    $agentPy = Join-Path $AgentDir "agent.py"
    if (-not (Test-Path $agentPy)) {
        # monorepo layout: agent/agent.py
        $alt = Join-Path $AgentDir "agent\agent.py"
        if (Test-Path $alt) { $agentPy = $alt; $AgentDir = Split-Path -Parent $alt }
    }
    if (-not (Test-Path $agentPy)) {
        throw "agent.py not found under $AgentDir — pass -Exe for .NET agent"
    }
    $cfg = Join-Path (Split-Path -Parent $agentPy) "agent.json"
    if (-not (Test-Path $cfg)) {
        $cfg = Join-Path $AgentDir "agent.json"
    }
    $py = (Get-Command $Python -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = $Python }
    $arg = "`"$agentPy`" loop -c `"$cfg`""
    $action = New-ScheduledTaskAction -Execute $py -Argument $arg -WorkingDirectory (Split-Path -Parent $agentPy)
}

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$triggers = @()
if ($AtLogon) {
    $triggers += New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
}

# Manual start only if no logon trigger (still runnable via schtasks /Run)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

if ($triggers.Count -gt 0) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
        -Settings $settings -Trigger $triggers -Force | Out-Null
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
        -Settings $settings -Force | Out-Null
}

Write-Host "Installed scheduled task: $TaskName (RunLevel Highest)"
Write-Host "Daily start (NO UAC prompt):"
Write-Host "  schtasks /Run /TN `"$TaskName`""
Write-Host "  or:  .\start-agent-silent.ps1"
Write-Host "Do not use Start-Process -Verb RunAs for daily launch — that always shows Yes/No."

#Requires -Version 5.1
<#
.SYNOPSIS
  Start the Hogwarts elevated agent without a UAC Yes/No prompt.

.DESCRIPTION
  Requires install-elevated-task.ps1 once (as Administrator). Then this script
  only runs the existing Highest task via the Task Scheduler service — no consent UI.

  If the task is missing, prints install instructions (does NOT fall back to RunAs,
  because RunAs always prompts).
#>
param(
    [string]$TaskName = "HogwartsAgentElevated"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Scheduled task '$TaskName' not found."
    Write-Host "One-time setup (elevated PowerShell — one UAC prompt for the install only):"
    Write-Host "  cd <hogwarts>\\agent\\windows"
    Write-Host "  .\\install-elevated-task.ps1 -AgentDir <path> [-Exe path\\to\\agent.exe] [-AtLogon]"
    Write-Host ""
    Write-Host "After that, this script starts elevated with no Yes/No."
    exit 2
}

# Already running?
$info = Get-ScheduledTaskInfo -TaskName $TaskName
if ($info.LastTaskResult -eq 267009 -or $task.State -eq "Running") {
    # 267009 = currently running (varies by OS); State is reliable
    if ($task.State -eq "Running") {
        Write-Host "Task '$TaskName' already Running (elevated)."
        exit 0
    }
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Milliseconds 400
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Started '$TaskName' state=$($task.State) — no UAC prompt (task is RunLevel Highest)."
exit 0

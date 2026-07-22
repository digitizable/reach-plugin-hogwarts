# COPE collectors — enumeration only (no exploit payloads)

function Invoke-CopeModule_token {
    param($Context)
    $findings = @()
    $il = Get-CopeIntegrityLabel
    $isAdmin = Test-CopeIsAdmin
    $privs = @()
    try {
        $raw = & whoami /priv 2>$null | Out-String
        foreach ($name in @(
                "SeImpersonatePrivilege", "SeAssignPrimaryTokenPrivilege",
                "SeDebugPrivilege", "SeTcbPrivilege", "SeBackupPrivilege",
                "SeRestorePrivilege", "SeLoadDriverPrivilege", "SeTakeOwnershipPrivilege",
                "SeCreateTokenPrivilege"
            )) {
            if ($raw -match [regex]::Escape($name)) {
                $enabled = $raw -match "$name\s+Privilege\s+Enabled"
                $privs += @{ name = $name; enabled_hint = $enabled }
            }
        }
    } catch {}

    $findings += New-CopeFinding -Id "token.integrity" -Title "Process integrity / admin role" `
        -Category "token" -Consent "none" -Confidence 0.85 `
        -Evidence @{ integrity_label = $il; is_admin_role = $isAdmin } `
        -Goals @{
            G1 = $(if ($isAdmin -or $il -match "High") { 1.0 } else { 0.05 })
            G3 = 1.0
            G2 = $(if ($il -match "System") { 0.8 } else { 0.0 })
        } `
        -NextSteps @(
            $(if (-not $isAdmin) { "Medium context: High inject blocked by UIPI until elevation path" } else { "High/admin: local SendInput should drive Task Manager class UI" })
        )

    $imp = $privs | Where-Object { $_.name -eq "SeImpersonatePrivilege" }
    if ($imp) {
        $findings += New-CopeFinding -Id "token.seimpersonate" -Title "SeImpersonatePrivilege present" `
            -Category "token" -Consent "none" -Confidence 0.7 `
            -Evidence @{ privileges = $privs } `
            -Goals @{ G2 = 0.75; G1 = 0.4; G3 = 0.9 } `
            -References @("impersonation → SYSTEM research class (PrintSpoofer-family taxonomy)") `
            -NextSteps @("Research track G2: token impersonation class — not auto-run by COPE")
    }

    $dbg = $privs | Where-Object { $_.name -eq "SeDebugPrivilege" }
    if ($dbg) {
        $findings += New-CopeFinding -Id "token.sedebug" -Title "SeDebugPrivilege present" `
            -Category "token" -Consent "none" -Confidence 0.65 `
            -Evidence @{ privileges = $privs } `
            -Goals @{ G2 = 0.5; G1 = 0.3; G3 = 0.9 } `
            -NextSteps @("Debug privilege expands process access; Control utility varies")
    }

    if ($privs.Count -gt 0) {
        $findings += New-CopeFinding -Id "token.privs" -Title "Interesting privileges snapshot" `
            -Category "token" -Consent "none" -Confidence 0.6 `
            -Evidence @{ privileges = $privs } `
            -Goals @{ G1 = 0.1; G2 = 0.2; G3 = 1.0 }
    }
    return $findings
}

function Invoke-CopeModule_process {
    param($Context)
    $sid = 0
    try {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class CopeSess {
  [DllImport("kernel32.dll")] public static extern uint WTSGetActiveConsoleSessionId();
}
"@ -ErrorAction SilentlyContinue
        $sid = [CopeSess]::WTSGetActiveConsoleSessionId()
    } catch {}
    return @(
        New-CopeFinding -Id "process.session" -Title "Interactive session context" `
            -Category "process" -Consent "none" -Confidence 0.7 `
            -Evidence @{
                pid = $PID
                session_id = $sid
                user = $env:USERNAME
            } `
            -Goals @{ G1 = 0.2; G3 = 1.0; G5 = 0.1 } `
            -NextSteps @("Inject/capture must land on interactive session desktop winsta0\\default")
    )
}

function Invoke-CopeModule_uac_policy {
    param($Context)
    $path = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
    $vals = @{}
    foreach ($n in @(
            "EnableLUA", "ConsentPromptBehaviorAdmin", "ConsentPromptBehaviorUser",
            "PromptOnSecureDesktop", "FilterAdministratorToken", "EnableSecureUIAPaths"
        )) {
        try { $vals[$n] = (Get-ItemProperty -Path $path -Name $n -ErrorAction SilentlyContinue).$n } catch {}
    }
    $enableLua = $vals["EnableLUA"]
    $consent = $vals["ConsentPromptBehaviorAdmin"]
    # ConsentPromptBehaviorAdmin: 0 = elevate without prompt (least secure), 2 = prompt on secure desktop, etc.
    $silentAdmin = ($consent -eq 0)
    return @(
        New-CopeFinding -Id "uac.policy" -Title "UAC policy snapshot (read-only)" `
            -Category "uac_policy" `
            -Consent $(if ($silentAdmin) { "none" } else { "every_use" }) `
            -Confidence 0.8 `
            -Evidence $vals `
            -Goals @{
                G3 = $(if ($silentAdmin) { 0.9 } elseif ($enableLua -eq 0) { 0.7 } else { 0.2 })
                G1 = 0.1
                G5 = $(if ($vals["PromptOnSecureDesktop"] -eq 1) { 0.4 } else { 0.2 })
            } `
            -NextSteps @(
                "Policy is host state — COPE does not change it",
                "ConsentPromptBehaviorAdmin=0 means admin elevation without prompt (research class)"
            )
    )
}

function Invoke-CopeModule_tasks {
    param($Context)
    $findings = @()
    try {
        $tasks = Get-ScheduledTask -ErrorAction SilentlyContinue
    } catch { $tasks = @() }

    $highest = @()
    foreach ($t in $tasks) {
        try {
            $p = $t.Principal
            if ($p -and $p.RunLevel -eq "Highest") {
                $highest += [ordered]@{
                    name = $t.TaskName
                    path = $t.TaskPath
                    state = "$($t.State)"
                    user = $p.UserId
                }
            }
        } catch {}
    }

    # Hogwarts known silent path
    $hw = $highest | Where-Object { $_.name -eq "HogwartsAgentElevated" }
    if ($hw) {
        $findings += New-CopeFinding -Id "tasks.hogwarts_elevated" `
            -Title "HogwartsAgentElevated Highest task present (silent B1)" `
            -Category "tasks" -Consent "once_at_install" -Confidence 0.95 `
            -Evidence @{ task = $hw } `
            -Goals @{ G1 = 0.95; G3 = 0.95; G6 = 0.85; G2 = 0.1 } `
            -References @("Anguish uac-bypasses B1", "start-agent-silent.ps1") `
            -NextSteps @("Daily: start-agent-silent.ps1 / schtasks /Run — no Yes/No")
    } else {
        $findings += New-CopeFinding -Id "tasks.hogwarts_missing" `
            -Title "HogwartsAgentElevated not installed" `
            -Category "tasks" -Consent "once_at_install" -Confidence 0.9 `
            -Evidence @{ hint = "install-elevated-task.ps1 once as admin" } `
            -Goals @{ G1 = 0.0; G3 = 0.0; G6 = 0.0 } `
            -NextSteps @("Research/install silent B1: agent/windows/install-elevated-task.ps1")
    }

    $other = @($highest | Where-Object { $_.name -ne "HogwartsAgentElevated" } | Select-Object -First 25)
    if ($other.Count -gt 0) {
        $findings += New-CopeFinding -Id "tasks.highest_others" `
            -Title ("Other Highest-runlevel tasks: {0}" -f $other.Count) `
            -Category "tasks" -Consent "unknown" -Confidence 0.55 `
            -Evidence @{ tasks = $other } `
            -Goals @{ G1 = 0.35; G3 = 0.5; G6 = 0.4 } `
            -NextSteps @("Audit whether current user can start these without consent (research)")
    }
    return $findings
}

function Invoke-CopeModule_services {
    param($Context)
    $findings = @()
    $interesting = @()
    try {
        $svcs = Get-CimInstance Win32_Service -ErrorAction SilentlyContinue |
            Where-Object { $_.StartMode -match "Auto|Manual" } |
            Select-Object -First 400
        foreach ($s in $svcs) {
            $path = [string]$s.PathName
            $acct = [string]$s.StartName
            $unquoted = $false
            if ($path -and $path -notmatch '^"' -and $path -match ' ') {
                # crude unquoted path with spaces
                $unquoted = $true
            }
            $systemish = $acct -match "LocalSystem|SYSTEM|LocalService|NetworkService"
            if ($unquoted -or ($systemish -and $path -match "ProgramData|Users\\Public|Temp")) {
                $interesting += [ordered]@{
                    name = $s.Name
                    state = $s.State
                    start = $s.StartMode
                    account = $acct
                    path = $path
                    unquoted_heuristic = $unquoted
                }
            }
        }
    } catch {}

    if ($interesting.Count -gt 0) {
        $findings += New-CopeFinding -Id "services.heuristics" `
            -Title ("Service path/account heuristics: {0} candidates" -f $interesting.Count) `
            -Category "services" -Consent "unknown" -Confidence 0.45 `
            -Evidence @{ candidates = @($interesting | Select-Object -First 30) } `
            -Goals @{ G2 = 0.5; G1 = 0.25; G3 = 0.6; G6 = 0.4 } `
            -NextSteps @("Manual ACL check required — heuristic only, not proof of privesc")
    } else {
        $findings += New-CopeFinding -Id "services.cleanish" `
            -Title "No crude unquoted/weak-path service heuristics fired" `
            -Category "services" -Consent "none" -Confidence 0.4 `
            -Evidence @{} -Goals @{ G1 = 0; G2 = 0; G3 = 1 }
    }
    return $findings
}

function Invoke-CopeModule_autoelevate {
    param($Context)
    # Presence enumeration of well-known autoElevate binaries (paths only).
    # No invocation — research taxonomy for UAC surface.
    $names = @(
        "fodhelper.exe", "computerdefaults.exe", "eventvwr.exe",
        "sdclt.exe", "slui.exe", "changepk.exe", "WSReset.exe"
    )
    $sys32 = Join-Path $env:WINDIR "System32"
    $present = @()
    foreach ($n in $names) {
        $p = Join-Path $sys32 $n
        if (Test-Path $p) {
            $present += @{ name = $n; path = $p }
        }
    }
    return @(
        New-CopeFinding -Id "autoelevate.present" `
            -Title ("Known autoElevate-class binaries present: {0}" -f $present.Count) `
            -Category "autoelevate" -Consent "unknown" -Confidence 0.5 `
            -Evidence @{ binaries = $present } `
            -Goals @{ G1 = 0.4; G3 = 0.55; G5 = 0.2 } `
            -References @("UAC research class — enum only in COPE v0") `
            -NextSteps @("Lab taxonomy O5 in Anguish uac-bypasses; no auto-invoke")
    )
}

function Invoke-CopeModule_hogwarts {
    param($Context)
    $findings = @()
    $candidates = @(
        (Join-Path $PSScriptRoot "..\..\agent.json"),
        (Join-Path $PSScriptRoot "..\agent.json"),
        (Join-Path (Get-Location) "agent.json"),
        (Join-Path $env:USERPROFILE "Desktop\Hogwarts.Agent\agent.json")
    )
    $cfgPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    $ip = $null
    if ($cfgPath) {
        try {
            $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
            $ip = $cfg.input_provider
        } catch {}
    }
    $hasIp = $false
    if ($ip -and $ip.enabled -ne $false -and ($ip.command -or $ip.pipe)) { $hasIp = $true }

    $findings += New-CopeFinding -Id "hogwarts.config" `
        -Title $(if ($cfgPath) { "agent.json found" } else { "agent.json not found near engine" }) `
        -Category "hogwarts" -Consent "none" -Confidence 0.7 `
        -Evidence @{ path = $cfgPath; input_provider = $ip } `
        -Goals @{
            G1 = $(if ($hasIp) { 0.7 } else { 0.1 })
            G3 = $(if ($hasIp -and $ip.kind -eq "pipe") { 0.85 } elseif ($hasIp) { 0.4 } else { 0.2 })
        } `
        -NextSteps @(
            $(if ($hasIp) { "input_provider configured — prefer kind=pipe for silent High inject" } else { "Optional: input_provider plug-in (B2) for operator High helper" })
        )

    $silent = Join-Path $PSScriptRoot "..\start-agent-silent.ps1"
    $install = Join-Path $PSScriptRoot "..\install-elevated-task.ps1"
    $findings += New-CopeFinding -Id "hogwarts.scripts" `
        -Title "Silent elevated scripts present" `
        -Category "hogwarts" -Consent "once_at_install" -Confidence 0.8 `
        -Evidence @{
            start_silent = (Test-Path $silent)
            install_task = (Test-Path $install)
        } `
        -Goals @{ G1 = 0.5; G3 = 0.7; G6 = 0.5 } `
        -NextSteps @("install once elevated → start-agent-silent daily")

    return $findings
}

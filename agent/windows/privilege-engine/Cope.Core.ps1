# COPE core — scoring + finding helpers (Control-Oriented Privilege Engine)

function Get-CopeWeights {
    return [ordered]@{
        G1 = 1.0   # High IL inject (Task Manager class)
        G2 = 0.8   # SYSTEM with desktop path
        G3 = 0.9   # Silent reuse (no Yes/No)
        G4 = 0.3   # Capture elevated pixels
        G5 = 0.5   # Secure Desktop
        G6 = 0.6   # Persist logon
    }
}

function Get-CopeConsentFactor {
    param([string]$Consent)
    switch ($Consent) {
        "none" { return 1.0 }
        "once_at_install" { return 0.85 }
        "every_use" { return 0.15 }
        default { return 0.5 }
    }
}

function New-CopeContext {
    param([switch]$Verify)
    return [pscustomobject]@{
        Verify     = [bool]$Verify
        IsWindows  = ($env:OS -match "Windows")
        Computer   = $env:COMPUTERNAME
        User       = $env:USERNAME
        Now        = Get-Date
    }
}

function New-CopeFinding {
    param(
        [string]$Id,
        [string]$Title,
        [string]$Category,
        [hashtable]$Evidence = @{},
        [ValidateSet("none", "once_at_install", "every_use", "unknown")]
        [string]$Consent = "unknown",
        [hashtable]$Goals = @{},
        [double]$Confidence = 0.5,
        [string[]]$References = @(),
        [string[]]$NextSteps = @()
    )
    $g = @{
        G1 = 0.0; G2 = 0.0; G3 = 0.0; G4 = 0.0; G5 = 0.0; G6 = 0.0
    }
    foreach ($k in $Goals.Keys) { $g[$k] = [double]$Goals[$k] }
    return [pscustomobject]@{
        id         = $Id
        title      = $Title
        category   = $Category
        evidence   = $Evidence
        consent    = $Consent
        goals      = $g
        confidence = [math]::Max(0.0, [math]::Min(1.0, $Confidence))
        references = @($References)
        next_steps = @($NextSteps)
        score      = 0.0
    }
}

function Add-CopeScore {
    param($Finding)
    $w = Get-CopeWeights
    $sum = 0.0
    foreach ($k in $w.Keys) {
        $gv = 0.0
        if ($Finding.goals.ContainsKey($k)) { $gv = [double]$Finding.goals[$k] }
        $sum += [double]$w[$k] * $gv
    }
    $cf = Get-CopeConsentFactor -Consent $Finding.consent
    $Finding.score = [math]::Round($sum * [double]$Finding.confidence * $cf, 4)
    return $Finding
}

function New-CopeMarkdown {
    param($Report)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine("# COPE report — $($Report.host) / $($Report.user)")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("- Engine: $($Report.engine) $($Report.version)")
    [void]$sb.AppendLine("- Generated: $($Report.generated)")
    [void]$sb.AppendLine("- Mode: $($Report.mode) (verify=$($Report.verify))")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("## Thesis")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine($Report.thesis)
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("## Top findings")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("| Score | Consent | Title |")
    [void]$sb.AppendLine("|------:|---------|-------|")
    foreach ($t in $Report.top) {
        [void]$sb.AppendLine("| $($t.score) | $($t.consent) | $($t.title) |")
    }
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("## All findings")
    [void]$sb.AppendLine("")
    foreach ($f in $Report.findings) {
        [void]$sb.AppendLine("### [$($f.score)] $($f.title)")
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("- id: ``$($f.id)``")
        [void]$sb.AppendLine("- category: $($f.category)")
        [void]$sb.AppendLine("- consent: **$($f.consent)**")
        [void]$sb.AppendLine("- confidence: $($f.confidence)")
        $g1 = $f.goals.G1; $g3 = $f.goals.G3
        [void]$sb.AppendLine("- goals: G1(inject)=$g1 G3(silent)=$g3")
        if ($f.next_steps -and $f.next_steps.Count) {
            [void]$sb.AppendLine("- next: $($f.next_steps -join '; ')")
        }
        [void]$sb.AppendLine("")
    }
    [void]$sb.AppendLine("---")
    [void]$sb.AppendLine("Anguish: notes/hogwarts/research/privilege-engine-v0.txt")
    return $sb.ToString()
}

function Test-CopeIsAdmin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $p = New-Object Security.Principal.WindowsPrincipal($id)
        return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Get-CopeIntegrityLabel {
    # Best-effort: whoami /groups or token elevation type
    try {
        $elev = Test-CopeIsAdmin
        if ($elev) { return "High (admin role present / elevated token likely)" }
        # Check mandatory label via whoami
        $out = & whoami /groups 2>$null | Out-String
        if ($out -match "High Mandatory") { return "High" }
        if ($out -match "Medium Mandatory") { return "Medium" }
        if ($out -match "Low Mandatory") { return "Low" }
        if ($out -match "System Mandatory") { return "System" }
        return "Medium-or-unknown"
    } catch { return "unknown" }
}

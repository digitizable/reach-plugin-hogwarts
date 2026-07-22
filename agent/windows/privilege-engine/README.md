# COPE — Control-Oriented Privilege Engine (v0)

Security research tool for Hogwarts lab hosts.

**Not** a UAC clicker. **Not** an exploit pack.  
**Does** enumerate privilege / elevation surfaces and **score** them for remote **Control** goals (High inject, silent reuse).

## Novel idea

Classic privesc tools optimize for a shell.  
COPE optimizes for: *can we drive elevated UI without Yes/No on reuse?*

See Anguish: `notes/hogwarts/research/privilege-engine-v0.txt`

## Run (Windows eng)

```powershell
cd <hogwarts>\agent\windows\privilege-engine
powershell -ExecutionPolicy Bypass -File .\Invoke-Cope.ps1 -Out .\cope-report.json
```

- Writes `cope-report.json` + `.md`
- **No UAC prompt** (read-only enumeration)
- `-Verify` only checks whether `HogwartsAgentElevated` task exists (does not elevate)

## Modules (v0)

| Module | Role |
|--------|------|
| token | IL, interesting privileges |
| process | session context |
| uac_policy | consent policy registry (read) |
| tasks | Highest scheduled tasks + Hogwarts silent task |
| services | crude unquoted / weak path heuristics |
| autoelevate | presence of known autoElevate-class binaries (no invoke) |
| hogwarts | agent.json input_provider + silent scripts |

## Output

Findings include `consent` (`none` | `once_at_install` | `every_use` | `unknown`) and goals G1–G6. Sorted by Control score.

## Next

- Embed as agent task `priv_surface` (optional)
- Lab verifiers behind explicit flag only
- Ingest external enum JSON and re-score with Control weights

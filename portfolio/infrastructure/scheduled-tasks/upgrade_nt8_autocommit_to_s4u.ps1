# One-time upgrade: re-register NT8AutoCommit as S4U + battery-safe, matching the
# NT8*Watchdog tasks. RUN ONCE IN AN ADMIN POWERSHELL (S4U needs elevation).
#
# Idempotent: -Force replaces the existing task, so re-running is harmless. The
# action, 5-min repetition, and the committed script are unchanged; only the
# principal (S4U/logged-out) and battery settings change.

#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$script = 'C:\Users\<user>\Documents\NinjaTrader 8\Maintenance\auto_commit_nt8_repo.ps1'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $script)

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)

# S4U = "run whether the user is logged on or not", no stored password. Same as
# the watchdog tasks. RunLevel Limited = no elevation for the commit itself.
$principal = New-ScheduledTaskPrincipal -UserId '<user>' -LogonType S4U -RunLevel Limited

# Battery-safe: start and keep running on battery, matching the NT8 watchdogs.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable

Register-ScheduledTask -TaskName 'NT8AutoCommit' `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Auto-commit the local top-level NT8 repo (source + BCDR docs) every 5 min. Local only, no push. Scoped to top-level repo; does NOT touch bin/Custom/Strategies.' `
    -Force | Out-Null

$t = Get-ScheduledTask -TaskName 'NT8AutoCommit'
Write-Host ('OK  State={0}  LogonType={1}  RunLevel={2}' -f `
    $t.State, $t.Principal.LogonType, $t.Principal.RunLevel)

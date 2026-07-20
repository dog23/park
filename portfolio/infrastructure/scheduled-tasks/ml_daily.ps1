$ErrorActionPreference = "SilentlyContinue"

$serviceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runScript = Join-Path $serviceDir "run_service.ps1"
$pythonExe = "C:\Users\<user>\AppData\Local\Programs\Python\Python313\python.exe"
$logPath = Join-Path $serviceDir "ml_daily.log"
$failMarker = Join-Path $serviceDir "ml_daily_fail.marker"
$alertMarker = Join-Path $serviceDir "ml_daily_alert.marker"
$healthUrl = "http://localhost:8765/health"
$ntfyTopic = "<ntfy-topic>"

function Write-WatchdogLog([string]$message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -LiteralPath $logPath -Value $line
}

function Send-Alert([string]$message, [string]$title) {
    try {
        Invoke-RestMethod -Uri "https://ntfy.sh/$ntfyTopic" -Method Post -Body $message -Headers @{ Title = $title; Priority = "urgent"; Tags = "rotating_light" } -TimeoutSec 5 | Out-Null
    } catch {
    }
}

try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 6
    if ($health.ok -eq $true) {
        if (Test-Path -LiteralPath $failMarker) {
            Remove-Item -LiteralPath $failMarker -Force
        }
        if (Test-Path -LiteralPath $alertMarker) {
            Remove-Item -LiteralPath $alertMarker -Force
            Send-Alert "MLService (port 8765) is back up." "NT8 Tema Limit ML: recovered"
        }
        Write-WatchdogLog ("OK model_version={0} n_features={1}" -f $health.model_version, $health.n_features)
        exit 0
    }
} catch {
}

if (!(Test-Path -LiteralPath $failMarker)) {
    Set-Content -LiteralPath $failMarker -Value (Get-Date -Format "o")
    Write-WatchdogLog "Health check failed once; waiting for next check before restart."
    exit 0
}

Write-WatchdogLog "Health check failed twice; restarting ML service."

try {
    $targetPids = New-Object System.Collections.Generic.HashSet[int]
    $connections = Get-NetTCPConnection -LocalPort 8765 -State Listen
    foreach ($connection in $connections) { [void]$targetPids.Add($connection.OwningProcess) }

    # SO_REUSEADDR lets a second uvicorn bind :8765 alongside an existing one (e.g.
    # this watchdog racing restart_service.ps1), leaving an orphan double-bound to
    # the port with stale model state. Kill every matching python process, not just
    # the PID currently reported as listening.
    $uvicornProcs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -match "uvicorn" -and $_.CommandLine -match "service:app" }
    foreach ($p in $uvicornProcs) { [void]$targetPids.Add($p.ProcessId) }

    # Retrains run in ProcessPoolExecutor children whose command line is a
    # multiprocessing spawn stub, not "uvicorn service:app", so the filter above
    # misses them and they survive with a dead parent, burning cores forever
    # (2026-07-19: two orphans held ~10 of 16 cores and starved the new service).
    # Reap the whole tree.
    $allProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Select-Object ProcessId, ParentProcessId
    if ($allProcs) {
        $queue = New-Object System.Collections.Generic.Queue[int]
        foreach ($seed in @($targetPids)) { $queue.Enqueue($seed) }
        while ($queue.Count -gt 0) {
            $current = $queue.Dequeue()
            foreach ($child in ($allProcs | Where-Object { $_.ParentProcessId -eq $current })) {
                if (-not $targetPids.Contains([int]$child.ProcessId)) {
                    [void]$targetPids.Add([int]$child.ProcessId)
                    $queue.Enqueue([int]$child.ProcessId)
                }
            }
        }
    }

    foreach ($targetPid in $targetPids) {
        try {
            Stop-Process -Id $targetPid -Force -ErrorAction Stop
            Write-WatchdogLog ("killed pid {0}" -f $targetPid)
        } catch {
            Write-WatchdogLog ("FAILED to kill pid {0} (likely session-0 isolated -- user must End Task it manually, will NOT retry): {1}" -f $targetPid, $_.Exception.Message)
        }
    }
} catch {
    Write-WatchdogLog ("process scan/kill failed: {0}" -f $_.Exception.Message)
}

Start-Sleep -Seconds 2
Start-Process -FilePath $pythonExe -WorkingDirectory $serviceDir -ArgumentList @("-m","uvicorn","service:app","--host","0.0.0.0","--port","8765") -WindowStyle Hidden

for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 6
        if ($health.ok -eq $true) {
            if (Test-Path -LiteralPath $failMarker) {
                Remove-Item -LiteralPath $failMarker -Force
            }
            Write-WatchdogLog ("Restart OK model_version={0} n_features={1}" -f $health.model_version, $health.n_features)
            exit 0
        }
    } catch {
    }
}

Write-WatchdogLog "Restart attempted but health check still failed after 45 seconds."
if (!(Test-Path -LiteralPath $alertMarker)) {
    Set-Content -LiteralPath $alertMarker -Value (Get-Date -Format "o")
    Send-Alert "MLService (port 8765) is down and the watchdog's restart attempt failed. Manual attention needed." "NT8 Tema Limit ML: DOWN"
}
exit 1



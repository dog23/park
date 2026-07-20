$ErrorActionPreference = "SilentlyContinue"

$serviceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = "C:\Users\<user>\AppData\Local\Programs\Python\Python313\python.exe"
$logPath = Join-Path $serviceDir "trend_watchdog.log"
$failMarker = Join-Path $serviceDir "trend_watchdog_fail.marker"
$alertMarker = Join-Path $serviceDir "trend_watchdog_alert.marker"
$healthUrl = "http://localhost:8767/health"
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
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
    if ($health.ok -eq $true) {
        if (Test-Path -LiteralPath $failMarker) {
            Remove-Item -LiteralPath $failMarker -Force
        }
        if (Test-Path -LiteralPath $alertMarker) {
            Remove-Item -LiteralPath $alertMarker -Force
            Send-Alert "MLService_Trend (port 8767) is back up." "NT8 Trend ML: recovered"
        }
        Write-WatchdogLog ("OK groups_known={0} groups_ready={1}" -f $health.groups_known, $health.groups_ready)
        exit 0
    }
} catch {
}

if (!(Test-Path -LiteralPath $failMarker)) {
    Set-Content -LiteralPath $failMarker -Value (Get-Date -Format "o")
    Write-WatchdogLog "Health check failed once; waiting for next check before restart."
    exit 0
}

Write-WatchdogLog "Health check failed twice; restarting Trend ML service."

try {
    $connections = Get-NetTCPConnection -LocalPort 8767 -State Listen
    foreach ($connection in $connections) {
        Stop-Process -Id $connection.OwningProcess -Force
    }
} catch {
}

Start-Sleep -Seconds 2
Start-Process -FilePath $pythonExe -WorkingDirectory $serviceDir -ArgumentList @("-m","uvicorn","app:app","--host","0.0.0.0","--port","8767") -WindowStyle Hidden

for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        if ($health.ok -eq $true) {
            if (Test-Path -LiteralPath $failMarker) {
                Remove-Item -LiteralPath $failMarker -Force
            }
            Write-WatchdogLog ("Restart OK groups_known={0} groups_ready={1}" -f $health.groups_known, $health.groups_ready)
            exit 0
        }
    } catch {
    }
}

Write-WatchdogLog "Restart attempted but health check still failed after 20 seconds."
if (!(Test-Path -LiteralPath $alertMarker)) {
    Set-Content -LiteralPath $alertMarker -Value (Get-Date -Format "o")
    Send-Alert "MLService_Trend (port 8767) is down and the watchdog's restart attempt failed. Manual attention needed." "NT8 Trend ML: DOWN"
}
exit 1

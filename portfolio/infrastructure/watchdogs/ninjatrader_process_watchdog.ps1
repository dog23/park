$ErrorActionPreference = "SilentlyContinue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logPath = Join-Path $scriptDir "ninjatrader_process_watchdog.log"
$alertMarker = Join-Path $scriptDir "ninjatrader_process_alert.marker"
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

$proc = Get-Process -Name "NinjaTrader" -ErrorAction SilentlyContinue | Select-Object -First 1

if ($proc -and $proc.Responding) {
    Write-WatchdogLog ("OK pid={0}" -f $proc.Id)
    if (Test-Path -LiteralPath $alertMarker) {
        Remove-Item -LiteralPath $alertMarker -Force
        Send-Alert "NinjaTrader is running and responding again." "NT8 NinjaTrader: recovered"
    }
    exit 0
}

if (-not $proc) {
    Write-WatchdogLog "NinjaTrader process not found (crashed or not running)."
} else {
    Write-WatchdogLog ("NinjaTrader process found (pid={0}) but not responding (frozen)." -f $proc.Id)
}

# Deliberately does NOT auto-restart NinjaTrader -- restarting a live trading
# platform unattended means reconnecting to the broker/data feed and
# re-enabling every strategy with no one confirming account/position state
# first. This watchdog only alerts; the user restarts it by hand.
if (!(Test-Path -LiteralPath $alertMarker)) {
    Set-Content -LiteralPath $alertMarker -Value (Get-Date -Format "o")
    Send-Alert "NinjaTrader has crashed or is frozen. It will NOT auto-restart -- go check it." "NT8 NinjaTrader: DOWN/FROZEN"
}

$ErrorActionPreference = "SilentlyContinue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$traceDir = Join-Path (Split-Path -Parent $scriptDir) "trace"
$logPath = Join-Path $scriptDir "trace_log_cleanup.log"
$alertMarker = Join-Path $scriptDir "trace_log_runaway_alert.marker"
$ntfyTopic = "<ntfy-topic>"
$retentionDays = 14
$runawaySizeBytes = 1GB

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

if (!(Test-Path -LiteralPath $traceDir)) {
    Write-WatchdogLog "Trace directory not found, skipping."
    exit 0
}

$cutoff = (Get-Date).AddDays(-$retentionDays)
$oldFiles = Get-ChildItem -LiteralPath $traceDir -File -Filter "trace.*.txt" | Where-Object { $_.LastWriteTime -lt $cutoff }
$deletedCount = 0
$deletedBytes = 0
foreach ($f in $oldFiles) {
    $deletedBytes += $f.Length
    $deletedCount++
    Remove-Item -LiteralPath $f.FullName -Force
}
if ($deletedCount -gt 0) {
    Write-WatchdogLog ("Deleted {0} trace file(s) older than {1} days, freed {2:N0} MB." -f $deletedCount, $retentionDays, ($deletedBytes / 1MB))
}

# Runaway-logging check: a single trace file over 1GB signals the same
# stuck-order retry loop / SQLite contention that caused the July 7 2026
# 310GB blowup -- alert same-day instead of waiting for the next cleanup.
$bigFiles = Get-ChildItem -LiteralPath $traceDir -File -Filter "trace.*.txt" | Where-Object { $_.Length -gt $runawaySizeBytes }

if ($bigFiles.Count -gt 0) {
    $names = ($bigFiles | ForEach-Object { "{0} ({1:N1} GB)" -f $_.Name, ($_.Length / 1GB) }) -join ", "
    Write-WatchdogLog ("Runaway trace file(s) detected: {0}" -f $names)
    if (!(Test-Path -LiteralPath $alertMarker)) {
        Set-Content -LiteralPath $alertMarker -Value (Get-Date -Format "o")
        Send-Alert "Trace log growing abnormally large: $names. Likely a stuck order retry loop -- check NinjaTrader." "NT8 Trace Log: runaway growth"
    }
} else {
    if (Test-Path -LiteralPath $alertMarker) {
        Remove-Item -LiteralPath $alertMarker -Force
        Send-Alert "Trace log sizes back to normal." "NT8 Trace Log: recovered"
    }
}

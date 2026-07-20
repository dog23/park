# Watches the park repo (including wireframes/) for file changes and
# runs autopush.ps1 shortly after activity settles. Runs indefinitely;
# meant to be launched by the ParkAutopushWatchdog scheduled task.

$ErrorActionPreference = "Stop"
$repoPath = $PSScriptRoot
$debounceSeconds = 20

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $repoPath
$watcher.IncludeSubdirectories = $true
$watcher.Filter = "*.*"
$watcher.EnableRaisingEvents = $true

$global:lastChange = $null

$action = {
    $path = $Event.SourceEventArgs.FullPath
    if ($path -match '\\\.git\\') { return }
    $global:lastChange = Get-Date
}

Register-ObjectEvent $watcher "Changed" -Action $action | Out-Null
Register-ObjectEvent $watcher "Created" -Action $action | Out-Null
Register-ObjectEvent $watcher "Deleted" -Action $action | Out-Null
Register-ObjectEvent $watcher "Renamed" -Action $action | Out-Null

while ($true) {
    Start-Sleep -Seconds 5
    if ($global:lastChange -and ((Get-Date) - $global:lastChange).TotalSeconds -ge $debounceSeconds) {
        $global:lastChange = $null
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoPath "autopush.ps1")
        } catch {}
    }
}

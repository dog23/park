$ErrorActionPreference = 'Stop'

$watchScript = 'C:\Users\<user>\Documents\NinjaTrader 8\Maintenance\watch_ninjatrader_strategies.ps1'
$logFile = 'C:\Users\<user>\Documents\NinjaTrader 8\Maintenance\watch_ninjatrader_strategies.log'

$running = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*watch_ninjatrader_strategies.ps1*" }

if (-not $running) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', "`"$watchScript`""
    ) -WindowStyle Hidden
    "$(Get-Date -Format s) watchdog restarted watcher (was not running)" | Out-File -FilePath $logFile -Append
}

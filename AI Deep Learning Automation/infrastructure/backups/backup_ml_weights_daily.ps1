$ErrorActionPreference = "Stop"

$ntRoot = "C:\Users\<user>\Documents\NinjaTrader 8"
$backupRoot = "C:\Users\<user>\Documents\NinjaTrader 8 Backup\ML_Weights_Daily"
$backupDocsRoot = "C:\Users\<user>\Documents\NinjaTrader 8 Backup"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dest = Join-Path $backupRoot $stamp
$weightsDest = Join-Path $dest "Weights"
$serviceDest = Join-Path $dest "MLService"
$dashboardServerSource = Join-Path $ntRoot "LiveDashboardServer"
$dashboardServerDest = Join-Path $dest "LiveDashboardServer"
$gexFetchSource = Join-Path $ntRoot "GEX_DataFetch"
$gexFetchDest = Join-Path $dest "GEX_DataFetch"
$maintenanceSource = Join-Path $ntRoot "Maintenance"
$maintenanceDest = Join-Path $dest "Maintenance"
$ignoredStrategiesSource = Join-Path $ntRoot "CodexIgnoredStrategies"
$ignoredStrategiesDest = Join-Path $dest "CodexIgnoredStrategies"
$strategyFilesDest = Join-Path $dest "Strategies"
$fullTwentiesSource = Join-Path $ntRoot "fulltwenties"
$fullTwentiesDest = Join-Path $dest "fulltwenties"

New-Item -ItemType Directory -Path $weightsDest -Force | Out-Null
New-Item -ItemType Directory -Path $serviceDest -Force | Out-Null
New-Item -ItemType Directory -Path $dashboardServerDest -Force | Out-Null
New-Item -ItemType Directory -Path $gexFetchDest -Force | Out-Null
New-Item -ItemType Directory -Path $strategyFilesDest -Force | Out-Null
New-Item -ItemType Directory -Path $maintenanceDest -Force | Out-Null
New-Item -ItemType Directory -Path $ignoredStrategiesDest -Force | Out-Null
New-Item -ItemType Directory -Path $fullTwentiesDest -Force | Out-Null

$copied = New-Object System.Collections.Generic.List[string]
$missing = New-Object System.Collections.Generic.List[string]

# TemaLimit_bb_vwap_tcnn.pt removed 2026-07-20: superseded by the per-group
# exit_model_*.pt / weights the services write, and absent since well before
# then -- it was one of the 11 permanent "missing" entries.
$weightFiles = @(
    "NQOnlineMLP_multi_weights.txt",
    "GlobalSpikeMemory_weights.txt"
)

$serviceSource = Join-Path $ntRoot "MLService"
$trendServiceSource = Join-Path $ntRoot "MLService_Trend"
$trendServiceDest = Join-Path $dest "MLService_Trend"
$strategiesSource = Join-Path $ntRoot "bin\Custom\Strategies"

New-Item -ItemType Directory -Path $trendServiceDest -Force | Out-Null

foreach ($name in $weightFiles) {
    $source = Join-Path $ntRoot $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $weightsDest $name) -Force
        Copy-Item -LiteralPath $source -Destination (Join-Path $dest $name) -Force
        $copied.Add($source)
    } else {
        $missing.Add($source)
    }
}

# Entry direction model files: glob for per-group temalimit entry models
if (Test-Path -LiteralPath $ntRoot) {
    Get-ChildItem -LiteralPath $ntRoot -Filter "TemaLimit_bb_vwap_tcnn_*.pt" -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $weightsDest $_.Name) -Force
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Force
        $copied.Add($_.FullName)
    }
    Get-ChildItem -LiteralPath $ntRoot -Filter "TemaLimit_bb_vwap_tcnn_*.json" -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $weightsDest $_.Name) -Force
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Force
        $copied.Add($_.FullName)
    }
    Get-ChildItem -LiteralPath $ntRoot -Filter "TemaLimit_bb_vwap_tcnn_*_history.jsonl" -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $weightsDest $_.Name) -Force
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Force
        $copied.Add($_.FullName)
    }
    # Template selection model files: same per-group layout as the entry models
    Get-ChildItem -LiteralPath $ntRoot -Filter "template_model_*.pt" -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $weightsDest $_.Name) -Force
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Force
        $copied.Add($_.FullName)
    }
    Get-ChildItem -LiteralPath $ntRoot -Filter "template_model_*.json" -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $weightsDest $_.Name) -Force
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Force
        $copied.Add($_.FullName)
    }
    Get-ChildItem -LiteralPath $ntRoot -Filter "template_model_*_history.jsonl" -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $weightsDest $_.Name) -Force
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Force
        $copied.Add($_.FullName)
    }
}
# Exit model files: glob for per-group exit models (exit_model_*.pt, .json, exit_samples_*.tsv)
if (Test-Path -LiteralPath $serviceSource) {
    Get-ChildItem -LiteralPath $serviceSource -Filter "exit_model_*.pt" -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $weightsDest $_.Name) -Force
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Force
        $copied.Add($_.FullName)
    }
    Get-ChildItem -LiteralPath $serviceSource -Filter "exit_model_*.json" -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $weightsDest $_.Name) -Force
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Force
        $copied.Add($_.FullName)
    }
    Get-ChildItem -LiteralPath $serviceSource -Filter "exit_samples_*.tsv" -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $weightsDest $_.Name) -Force
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Force
        $copied.Add($_.FullName)
    }
}

if (Test-Path -LiteralPath $serviceSource) {
    Get-ChildItem -LiteralPath $serviceSource -Force | Where-Object {
        $_.Name -ne "__pycache__" -and $_.Name -notlike "*.bak_*"
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $serviceDest -Recurse -Force
        $copied.Add($_.FullName)
    }
} else {
    $missing.Add($serviceSource)
}

# MLService_Trend folder: TCN trend model service, port 8767, includes
# weights/ (per-symbol-dataseries checkpoints), data/, app.py, trend_model.py,
# trend_utils.py, run/start/watchdog scripts.
if (Test-Path -LiteralPath $trendServiceSource) {
    Get-ChildItem -LiteralPath $trendServiceSource -Force | Where-Object {
        $_.Name -ne "__pycache__" -and $_.Name -notlike "*.bak_*"
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $trendServiceDest -Recurse -Force
        $copied.Add($_.FullName)
    }
} else {
    $missing.Add($trendServiceSource)
}

# Live dashboard server (port 8766, dashboard.html + live_dashboard_server.py).
# Lives under NinjaTrader 8\LiveDashboardServer alongside MLService and
# MLService_Trend; not regenerable from the ML backup alone, and it's the
# only thing serving the completed-trade dashboard, so it gets backed up here too.
if (Test-Path -LiteralPath $dashboardServerSource) {
    Get-ChildItem -LiteralPath $dashboardServerSource -Force | Where-Object {
        $_.Name -ne "__pycache__" -and $_.Name -notlike "*.bak_*"
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $dashboardServerDest -Recurse -Force
        $copied.Add($_.FullName)
    }
} else {
    $missing.Add($dashboardServerSource)
}

# temalimit.cs strategy file (refactored with entry/exit diagnostics)
$temalimitSource = Join-Path $strategiesSource "temalimit.cs"
if (Test-Path -LiteralPath $temalimitSource) {
    Copy-Item -LiteralPath $temalimitSource -Destination (Join-Path $dest "temalimit.cs") -Force
    $copied.Add($temalimitSource)
} else {
    $missing.Add($temalimitSource)
}

# TrendTcnStrategy.cs strategy file (Donchian/SuperTrend/ADX/Choppiness/RelVol
# gate + Order Flow Delta/ATR ML features, TCN service on port 8767)
$trendTcnStrategySource = Join-Path $strategiesSource "TrendTcnStrategy.cs"
if (Test-Path -LiteralPath $trendTcnStrategySource) {
    Copy-Item -LiteralPath $trendTcnStrategySource -Destination (Join-Path $dest "TrendTcnStrategy.cs") -Force
    $copied.Add($trendTcnStrategySource)
} else {
    $missing.Add($trendTcnStrategySource)
}

# GEX.cs block removed 2026-07-20: the strategy file was deleted from
# Strategies\ (only temalimit.cs and TrendTcnStrategy.cs are traded) and its
# absence was the last permanent entry in the "missing" count. The GEX_DataFetch
# folder is still backed up below -- that one exists and is still producing
# levels CSVs; only the dead .cs reference is gone. History for GEX.cs remains
# in Strategies_git_history.bundle if it is ever needed again.


# Compile-visible NinjaScript strategy/source files. This is the current
# keep-list maintained by Maintenance\maintain_ninjatrader_strategies.ps1.
$activeStrategyFiles = @(
    # Only the two actively-traded strategies and what they depend on. The nine
    # entries removed 2026-07-20 (cerave, fulltwenties, GEX,
    # GlobalSpikeMLLadderStrategy, marketmultiticker, multidataseries,
    # NQOnlineMLP, temamarket, twentyfourseven) were deleted from Strategies\
    # long ago but left here, pinning "missing" at 11 every single day. A count
    # that never reaches 0 cannot be read as an alarm -- a real failure would
    # have shown as missing=12 and been indistinguishable from the noise. Their
    # history is still recoverable from Strategies_git_history.bundle.
    "@Strategy.cs",
    "ActiveStopVisualStrategyBase.cs",
    # Helper/exporter classes temalimit.cs and TrendTcnStrategy.cs compile
    # against. Never on this list, so they were only ever captured via the git
    # bundle (committed state) -- uncommitted edits to them were not backed up.
    "ManualCancelCommand.cs",
    "ManualExitCommand.cs",
    "OpenTradeStatusExporter.cs",
    "PendingTradeStatusExporter.cs",
    "PullbackStateExporter.cs",
    "temalimit.cs",
    "TrendTcnStrategy.cs"
)

foreach ($name in $activeStrategyFiles) {
    $source = Join-Path $strategiesSource $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $strategyFilesDest $name) -Force
        $copied.Add($source)
    } else {
        $missing.Add($source)
    }
}
# Full git history bundle for the strategies repo (bin\Custom\Strategies), so
# the StrategyAutoCommit rollback commits survive a disk loss -- the file
# snapshots above only capture today's version, not the commit history.
$strategiesGitBundleDest = Join-Path $dest "Strategies_git_history.bundle"
if (Test-Path -LiteralPath (Join-Path $strategiesSource ".git")) {
    Push-Location $strategiesSource
    try {
        git bundle create $strategiesGitBundleDest --all 2>&1 | Out-Null
        if (Test-Path -LiteralPath $strategiesGitBundleDest) {
            $copied.Add($strategiesGitBundleDest)
        } else {
            $missing.Add("$strategiesSource\.git (bundle create failed)")
        }
    } finally {
        Pop-Location
    }
} else {
    $missing.Add("$strategiesSource\.git")
}

# Same treatment for the NT8-root repo added 2026-07-20, which version-controls
# the five service folders (MLService, MLService_Trend, LiveDashboardServer,
# Maintenance, hardware_monitor) plus GEX_DataFetch and ML_SYSTEM_GUIDE.txt --
# none of which had any version control before it.
#
# This bundle is the ONLY way that history leaves the machine. The per-folder
# copies above capture today's working files, and the repo itself is local with
# no remote (deliberately -- see the root .gitignore), so without this a disk
# failure loses every commit while keeping the current files. Bundling costs
# nothing: the repo is ~1.1 MB of source.
$rootGitBundleDest = Join-Path $dest "NT8Root_git_history.bundle"
if (Test-Path -LiteralPath (Join-Path $ntRoot ".git")) {
    Push-Location $ntRoot
    try {
        git bundle create $rootGitBundleDest --all 2>&1 | Out-Null
        if (Test-Path -LiteralPath $rootGitBundleDest) {
            $copied.Add($rootGitBundleDest)
        } else {
            $missing.Add("$ntRoot\.git (bundle create failed)")
        }
    } finally {
        Pop-Location
    }
} else {
    $missing.Add("$ntRoot\.git")
}

# --- Scheduled task definitions (BCDR gap 1, closed 2026-07-20) ---
# The entire automation layer is Windows scheduled tasks: 11 of them at the time
# of writing (service watchdogs for 8765/8766/8767, NinjaTrader process watchdog,
# circuit breaker, naked-position watchdog, hardware monitor, trace-log cleanup,
# the two backup tasks, park autopush). Their SCRIPTS were already backed up, but
# their REGISTRATIONS were not -- so a rebuild produced working scripts that
# nothing ever ran, and only 3 of the 11 had a setup_*_task.bat to recreate them.
# Triggers, S4U principal, run level and repetition intervals would all have had
# to be reconstructed from memory, assuming you even remembered a task existed.
#
# Filter is "the action references the NT8 root" rather than a name pattern: it
# is self-maintaining (a new task is picked up automatically) and it will not
# sweep in Windows' own Backup / MareBackup / RegIdleBackup tasks.
# Export-ScheduledTask is read-only and does NOT require elevation (unlike
# Disable/Register, which do).
$tasksDest = Join-Path $dest "ScheduledTasks"
New-Item -ItemType Directory -Path $tasksDest -Force | Out-Null
$taskExportCount = 0
try {
    $ntTasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
        $act = ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " "
        $act -like "*NinjaTrader 8*"
    }
    foreach ($task in $ntTasks) {
        $safeName = ($task.TaskName -replace '[^\w\.\-]', '_')
        $xmlPath = Join-Path $tasksDest "$safeName.xml"
        try {
            $xmlText = Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
            # MUST be Unicode (UTF-16 LE), not utf8: Export-ScheduledTask emits a
            # header declaring encoding="UTF-16", and `schtasks /Create /XML`
            # reads the file directly and honours that declaration -- a UTF-8 file
            # claiming UTF-16 fails to import. Register-ScheduledTask via
            # Get-Content -Raw would have tolerated the mismatch, so this only
            # surfaces on the restore path that uses schtasks. Found 2026-07-20.
            $xmlText | Out-File -LiteralPath $xmlPath -Encoding Unicode
            if (Test-Path -LiteralPath $xmlPath) {
                $copied.Add($xmlPath)
                $taskExportCount++
            }
        } catch {
            $missing.Add("scheduled task export: $($task.TaskName)")
        }
    }
    # A restore note beside the XMLs, so the import command does not have to be
    # remembered or rediscovered under pressure.
    $readme = @"
Scheduled task definitions for the NT8 trading stack.
Exported automatically by MLService\backup_ml_weights_daily.ps1.

These are the task REGISTRATIONS. The scripts they run are backed up separately
(MLService\, MLService_Trend\, LiveDashboardServer\, Maintenance\, hardware_monitor\).
Restore the folders FIRST, to the same paths, then re-register these.

To restore one (elevated PowerShell):
  Register-ScheduledTask -Xml (Get-Content 'NT8MLServiceWatchdog.xml' -Raw) -TaskName 'NT8MLServiceWatchdog' -User '<user>' -Password '<pw>'

To restore all:
  Get-ChildItem *.xml | ForEach-Object {
      Register-ScheduledTask -Xml (Get-Content `$_.FullName -Raw) -TaskName `$_.BaseName -User '<user>' -Password '<pw>'
  }

Most run as S4U (run whether logged on or not), which is why -User/-Password is
needed at registration time. Paths inside the XML are absolute -- if the rebuild
uses a different user or drive, edit them before registering.
"@
    $readmePath = Join-Path $tasksDest "RESTORE_README.txt"
    $readme | Out-File -LiteralPath $readmePath -Encoding utf8
    if (Test-Path -LiteralPath $readmePath) { $copied.Add($readmePath) }
} catch {
    $missing.Add("scheduled task export (enumeration failed)")
}

# --- rclone config (BCDR gap 2, closed 2026-07-20) ---
# Circular dependency without this: rclone.conf holds the MEGA remote definition
# and credentials, and lived ONLY on the machine being backed up -- so restoring
# FROM MEGA required a config that died WITH the laptop. Recoverable by hand via
# MEGA's web UI plus a fresh `rclone config`, but that is friction at the worst
# possible moment.
# NOTE this file contains an obscured MEGA password. It is going to the user's
# own private MEGA account alongside trade history, so the exposure is not new --
# but if that is unwanted, delete this block and reconfigure rclone by hand on a
# rebuild instead.
$rcloneConf = Join-Path $env:APPDATA "rclone\rclone.conf"
if (Test-Path -LiteralPath $rcloneConf) {
    $rcloneDest = Join-Path $dest "rclone.conf"
    Copy-Item -LiteralPath $rcloneConf -Destination $rcloneDest -Force
    if (Test-Path -LiteralPath $rcloneDest) { $copied.Add($rcloneConf) } else { $missing.Add($rcloneConf) }
} else {
    $missing.Add($rcloneConf)
}

# GEX_DataFetch folder (Fetch-GexLevels.ps1, pulls Unusual Whales API data).
# The UW_API_KEY credential itself is a user environment variable, not a
# file -- not backed up here; see ML_SYSTEM_GUIDE.txt PART 3.
if (Test-Path -LiteralPath $gexFetchSource) {
    Get-ChildItem -LiteralPath $gexFetchSource -Force | Where-Object {
        $_.Name -notlike "*.bak_*"
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $gexFetchDest -Recurse -Force
        $copied.Add($_.FullName)
    }
} else {
    $missing.Add($gexFetchSource)
}


# Strategy maintenance script and ignored strategy archive. These are outside
# NinjaTrader's normal compile folder but are needed to recover the current
# "only active strategies are visible/compiled" setup.
if (Test-Path -LiteralPath $maintenanceSource) {
    Get-ChildItem -LiteralPath $maintenanceSource -Force | Where-Object {
        $_.Name -notlike "*.bak_*"
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $maintenanceDest -Recurse -Force
        $copied.Add($_.FullName)
    }
} else {
    $missing.Add($maintenanceSource)
}

if (Test-Path -LiteralPath $ignoredStrategiesSource) {
    Get-ChildItem -LiteralPath $ignoredStrategiesSource -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $ignoredStrategiesDest -Recurse -Force
        $copied.Add($_.FullName)
    }
}

if (Test-Path -LiteralPath $fullTwentiesSource) {
    Get-ChildItem -LiteralPath $fullTwentiesSource -Force | Where-Object {
        $_.Name -notlike "*.bak_*"
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $fullTwentiesDest -Recurse -Force
        $copied.Add($_.FullName)
    }
}
$extraFiles = @(
    "GlobalSpikeMLLadderStrategy_log.csv",
    "NQOnlineMLP_trades.tsv",
    "NQ_MeanReversion_trades.tsv",
    "TemaLimit_completed_trades.tsv",
    "GlobalSpike_completed_trades.tsv",
    "TrendTcn_completed_trades.tsv",
    "Cerave_completed_trades.tsv",
    "MarketMultiTicker_completed_trades.tsv",
    "TemaMarket_completed_trades.tsv",
    "GEX_Levels_SPY.csv",
    "GEX_Levels_QQQ.csv",
    "GEX_Levels_IWM.csv",
    "GEX_Levels_DIA.csv"
)

foreach ($name in $extraFiles) {
    $source = Join-Path $ntRoot $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $dest $name) -Force
        $copied.Add($source)
    }
}

# Per-file source roots. ML_SYSTEM_GUIDE.txt is the LIVE, git-tracked guide at the
# NT8 root -- the one that actually gets edited. This used to copy it from
# $backupDocsRoot instead, where a stale sidecar had drifted to 2026-07-18, so the
# recovery doc shipped in every off-site backup was days out of date (the fresh
# guide only reached MEGA via NT8Root_git_history.bundle). profit-ladder-split-risk.html
# exists ONLY under $backupDocsRoot, so it stays sourced from there. Fixed 2026-07-20.
$backupDocSources = [ordered]@{
    "ML_SYSTEM_GUIDE.txt"           = (Join-Path $ntRoot "ML_SYSTEM_GUIDE.txt")
    "profit-ladder-split-risk.html" = (Join-Path $backupDocsRoot "profit-ladder-split-risk.html")
}

foreach ($name in $backupDocSources.Keys) {
    $source = $backupDocSources[$name]
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $dest $name) -Force
        $copied.Add($source)
    } else {
        $missing.Add($source)
    }
}

$manifest = Join-Path $dest "manifest.txt"
$lines = @()
$lines += "ML daily disaster-recovery backup"
$lines += "Created: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$lines += "Destination: $dest"
$lines += ""
$lines += "Includes files NinjaTrader .nt8bk does not reliably include:"
$lines += "- TemaLimit_bb_vwap_tcnn.pt"
$lines += "- TemaLimit_bb_vwap_tcnn_*.pt, .json, *_history.jsonl (per-symbol-dataseries entry models)"
$lines += "- template_model_*.pt, .json, *_history.jsonl (per-symbol-dataseries template selection models)"
$lines += "- NQOnlineMLP_multi_weights.txt"
$lines += "- GlobalSpikeMemory_weights.txt"
$lines += "- exit_model_*.pt, exit_model_*.json (per-symbol-dataseries models)"
$lines += "- exit_samples_*.tsv (per-symbol-dataseries training data)"
$lines += "- MLService folder, including ml_model.py, exit_model.py, service.py,"
$lines += "  feature_utils.py, run_service.ps1, ml_daily.ps1, data/, logs/"
$lines += "- MLService_Trend folder (TCN trend model, port 8767), including"
$lines += "  app.py, trend_model.py, trend_utils.py, weights/, data/,"
$lines += "  run_trend_service.ps1, trend_start_once.ps1, trend_watchdog.ps1"
$lines += "- LiveDashboardServer folder (port 8766 completed-trade dashboard),"
$lines += "  source: C:\Users\<user>\Documents\NinjaTrader 8\LiveDashboardServer\,"
$lines += "  including dashboard.html, live_dashboard_server.py, launcher/watchdog scripts"
$lines += "- profit-ladder-split-risk.html (standalone reference tool),"
$lines += "  source: C:\Users\<user>\Documents\NinjaTrader 8 Backup\profit-ladder-split-risk.html"
$lines += "- Strategies folder with current compile-visible NinjaScript keep-list:"
$lines += "  @Strategy.cs, ActiveStopVisualStrategyBase.cs, cerave.cs, fulltwenties.cs,"
$lines += "  GEX.cs, GlobalSpikeMLLadderStrategy.cs, marketmultiticker.cs,"
$lines += "  multidataseries.cs, NQOnlineMLP.cs, temalimit.cs, temamarket.cs,"
$lines += "  TrendTcnStrategy.cs, twentyfourseven.cs"
$lines += "- Root compatibility copies of temalimit.cs, TrendTcnStrategy.cs, GEX.cs"
$lines += "- GEX_DataFetch folder (Fetch-GexLevels.ps1; UW_API_KEY credential itself"
$lines += "  is a user env var, not a file -- not backed up here, see ML_SYSTEM_GUIDE.txt)"
$lines += "- GEX_Levels_SPY/QQQ/IWM/DIA.csv when present (GEX.cs's actual level data)"
$lines += "- Maintenance folder, including maintain_ninjatrader_strategies.ps1"
$lines += "- CodexIgnoredStrategies archive when present (not compiled by NinjaTrader)"
$lines += "- fulltwenties folder when present (strategy logs/data)"
$lines += "- GlobalSpikeMLLadderStrategy_log.csv when present"
$lines += "- NQOnlineMLP_trades.tsv when present"
$lines += "- NQ_MeanReversion_trades.tsv when present"
$lines += "- TemaLimit_completed_trades.tsv when present"
$lines += "- GlobalSpike_completed_trades.tsv when present"
$lines += "- TrendTcn_completed_trades.tsv when present"
$lines += "- Cerave_completed_trades.tsv when present"
$lines += "- MarketMultiTicker_completed_trades.tsv when present"
$lines += "- TemaMarket_completed_trades.tsv when present"
$lines += "- ML_SYSTEM_GUIDE.txt (architecture + restore instructions, combined)"
$lines += ""
$lines += "Copied:"
$lines += $copied
$lines += ""
$lines += "Missing:"
$lines += $missing
$lines | Set-Content -LiteralPath $manifest

# Keep only the 4 most recent timestamped backup folders.
# This prevents daily ML backups from growing forever while preserving recent recovery points.
$retentionCount = 4
$oldBackups = Get-ChildItem -LiteralPath $backupRoot -Directory -Force |
    Where-Object { $_.Name -match '^\d{8}_\d{6}$' } |
    Sort-Object Name -Descending |
    Select-Object -Skip $retentionCount

foreach ($oldBackup in $oldBackups) {
    $resolvedBackupRoot = (Resolve-Path -LiteralPath $backupRoot).Path
    $resolvedOldBackup = (Resolve-Path -LiteralPath $oldBackup.FullName).Path
    if ($resolvedOldBackup.StartsWith($resolvedBackupRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedOldBackup -Recurse -Force
    }
}
$logPath = Join-Path $backupRoot "daily_backup.log"
Add-Content -LiteralPath $logPath -Value ("{0} copied={1} missing={2} dest={3}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $copied.Count, $missing.Count, $dest)

Write-Output "backup=$dest"
Write-Output "copied=$($copied.Count)"
Write-Output "missing=$($missing.Count)"
Write-Output "retention_kept=$retentionCount"
Write-Output "retention_deleted=$($oldBackups.Count)"




$ErrorActionPreference = 'Stop'

$ntRoot = 'C:\Users\<user>\Documents\NinjaTrader 8'
$strategyDir = Join-Path $ntRoot 'bin\Custom\Strategies'
$backupArchiveRoot = Join-Path $ntRoot 'AiArchivedStrategies\Backup'

New-Item -ItemType Directory -Force -Path $backupArchiveRoot | Out-Null

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'

function Get-BackupGroupName {
    param([string]$Name)
    if ($Name -match '^(.*?\.cs)\.bak') {
        return $matches[1]
    } elseif ($Name -match '^(.*?)_20\d{6}') {
        return $matches[1]
    } else {
        return [System.IO.Path]::GetFileNameWithoutExtension($Name)
    }
}

# Move all backup files out of the compile folder into the archive.
$backupFiles = Get-ChildItem -LiteralPath $strategyDir -File |
    Where-Object { $_.Name -match '\.bak|backup|_20\d{6}|\.bak_' }

$archivedBackups = @()
foreach ($file in $backupFiles) {
    $groupName = Get-BackupGroupName $file.Name
    $groupDest = Join-Path $backupArchiveRoot $groupName
    New-Item -ItemType Directory -Force -Path $groupDest | Out-Null
    $target = Join-Path $groupDest $file.Name
    Move-Item -LiteralPath $file.FullName -Destination $target -Force
    $archivedBackups += $file.Name
}

# Trim each per-strategy archive folder down to the 4 newest backups; delete the rest.
$deletedFromArchive = @()
Get-ChildItem -LiteralPath $backupArchiveRoot -Directory | ForEach-Object {
    $archiveOld = Get-ChildItem -LiteralPath $_.FullName -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 4
    foreach ($file in $archiveOld) {
        Remove-Item -LiteralPath $file.FullName -Force
        $deletedFromArchive += $file.Name
    }
}

[pscustomobject]@{
    Timestamp = $stamp
    ArchivedOldBackups = $archivedBackups.Count
    DeletedFromArchive = $deletedFromArchive.Count
    BackupArchiveFolder = $backupArchiveRoot
} | Format-List

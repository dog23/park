$ErrorActionPreference = "Stop"

$rclone = "C:\Users\<user>\AppData\Local\Microsoft\WinGet\Packages\Rclone.Rclone_Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.74.3-windows-amd64\rclone.exe"
$remoteName = "mega"
$backupRoot = "C:\Users\<user>\Documents\NinjaTrader 8 Backup\ML_Weights_Daily"
$logPath = "C:\Users\<user>\Documents\NinjaTrader 8\MLService\upload_ml_backup_to_mega.log"
$markerName = ".uploaded_to_mega"
$ntfyTopic = "<ntfy-topic>"
$alertMarker = "C:\Users\<user>\Documents\NinjaTrader 8\MLService\.mega_stale_alert.marker"

# Alert if the newest backup folder has not uploaded successfully in this long.
# 26h rather than 24h so a normal day's 14:00 run never trips it.
$staleAlertHours = 26
# Re-alert cadence once stale, so it nags daily instead of only once.
$alertRepeatHours = 24

# Keep only this many dated folders on the remote. Each daily folder is a full
# ~1 GB upload and nothing pruned them before, so MEGA would have filled in
# roughly a month (50 GiB total, 48 free as of 2026-07-20).
$retainCount = 3

function Write-UploadLog([string]$message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -LiteralPath $logPath -Value $line
}

function Send-Alert([string]$message, [string]$title) {
    try {
        Invoke-RestMethod -Uri "https://ntfy.sh/$ntfyTopic" -Method Post -Body $message -Headers @{ Title = $title; Priority = "urgent"; Tags = "rotating_light" } -TimeoutSec 5 | Out-Null
    } catch {
    }
}

# Fingerprint = file count + total bytes. Replaces the old binary "already
# uploaded" marker, which stamped a folder once and then skipped it forever --
# so anything added afterwards (notably the manually-created NT8 .nt8bk export,
# which is how the NT8 backup actually gets made) would never upload, silently.
# Comparing a fingerprint means a later addition changes the total and triggers
# a fresh upload. rclone copy is incremental, so the re-run only transfers what
# is new rather than the whole folder again.
function Get-FolderFingerprint([string]$path) {
    $files = Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne $markerName }
    if (-not $files) { return "0|0" }
    $sum = ($files | Measure-Object -Property Length -Sum).Sum
    return ("{0}|{1}" -f $files.Count, $sum)
}

function Test-ShouldAlert {
    # Rate-limit the phone alert so a long outage does not spam.
    if (-not (Test-Path -LiteralPath $alertMarker)) { return $true }
    try {
        $last = Get-Item -LiteralPath $alertMarker
        return ((Get-Date) - $last.LastWriteTime).TotalHours -ge $alertRepeatHours
    } catch {
        return $true
    }
}

function Invoke-RemoteRetention {
    # Deliberately called ONLY after a confirmed successful upload -- never
    # delete an old copy until a newer one is verifiably on the remote.
    try {
        $listing = & $rclone lsf "${remoteName}:NinjaTrader8_Backups" --dirs-only 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $listing) {
            Write-UploadLog "Retention skipped: could not list remote."
            return
        }
        # Match the dated folder pattern only, so anything else stored under
        # NinjaTrader8_Backups is never touched. Names sort chronologically.
        $names = @($listing |
            ForEach-Object { $_.TrimEnd('/') } |
            Where-Object { $_ -match '^\d{8}_\d{6}$' } |
            Sort-Object -Descending)
        if ($names.Count -le $retainCount) { return }
        foreach ($old in ($names | Select-Object -Skip $retainCount)) {
            & $rclone purge "${remoteName}:NinjaTrader8_Backups/$old" 2>$null
            if ($LASTEXITCODE -eq 0) {
                Write-UploadLog "Retention: purged remote $old (keeping newest $retainCount)."
            } else {
                Write-UploadLog "Retention: FAILED to purge remote $old (rclone exit $LASTEXITCODE)."
            }
        }
    } catch {
        Write-UploadLog "Retention error: $_"
    }
}

function Invoke-StalenessCheck([string]$folderName, [string]$folderPath) {
    # The old script exit-0'd on every skip, so 288 consecutive failures raised
    # nothing at all. Anything that can stop the off-site copy has to be able to
    # reach the phone, or it is not a backup.
    $mp = Join-Path $folderPath $markerName
    $lastUpload = $null
    if (Test-Path -LiteralPath $mp) {
        try { $lastUpload = (Get-Item -LiteralPath $mp).LastWriteTime } catch { }
    }
    $ageHours = if ($lastUpload) { ((Get-Date) - $lastUpload).TotalHours } else { [double]::MaxValue }
    if ($ageHours -ge $staleAlertHours) {
        if (Test-ShouldAlert) {
            $detail = if ($lastUpload) { "last upload {0:yyyy-MM-dd HH:mm}" -f $lastUpload } else { "never uploaded" }
            Send-Alert "Off-site backup stale: $folderName ($detail). ML services + strategies are NOT off this machine." "NT8 backup not uploading"
            Set-Content -LiteralPath $alertMarker -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
            Write-UploadLog "ALERT sent: $folderName stale ($detail)."
        }
    } elseif (Test-Path -LiteralPath $alertMarker) {
        Remove-Item -LiteralPath $alertMarker -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $backupRoot)) {
    Write-UploadLog "Backup root not found: $backupRoot"
    if (Test-ShouldAlert) {
        Send-Alert "Backup root missing: $backupRoot" "NT8 backup not uploading"
        Set-Content -LiteralPath $alertMarker -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    }
    exit 0
}

$latest = Get-ChildItem -LiteralPath $backupRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1
if (-not $latest) {
    Write-UploadLog "No backup folders found under $backupRoot"
    if (Test-ShouldAlert) {
        Send-Alert "No backup folders under $backupRoot" "NT8 backup not uploading"
        Set-Content -LiteralPath $alertMarker -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    }
    exit 0
}

$markerPath = Join-Path $latest.FullName $markerName
$current = Get-FolderFingerprint $latest.FullName

$previousFingerprint = $null
if (Test-Path -LiteralPath $markerPath) {
    try {
        $raw = (Get-Content -LiteralPath $markerPath -Raw).Trim()
        # Marker format is "<timestamp>|<count>|<bytes>". A bare timestamp is
        # the previous version's format -- treat it as no fingerprint so the
        # folder re-uploads once, then carries a proper one.
        $parts = $raw -split '\|'
        if ($parts.Count -ge 3) { $previousFingerprint = "{0}|{1}" -f $parts[1], $parts[2] }
    } catch { }
}

if ($previousFingerprint -eq $current) {
    Invoke-StalenessCheck $latest.Name $latest.FullName
    # Prune on this path too, not only after an upload. Retention tied solely to
    # upload events leaves a backlog sitting on the remote until contents next
    # change -- and the marker's existence already proves a good copy is up.
    Invoke-RemoteRetention
    exit 0
}

# NOTE: deliberately no .zip/.nt8bk requirement any more. That gate meant the
# daily automated backup (models, exit-sample TSVs, template ledgers, trade
# history, and all five non-git service folders) only left the machine when a
# manual NT8 export happened to sit in the same folder -- last on 2026-07-17,
# while the log recorded 288 silent skips. Manual exports still upload; they
# are just no longer the only trigger.
$reason = if ($previousFingerprint) { "contents changed" } else { "not yet uploaded" }
Write-UploadLog "Uploading $($latest.Name) ($reason; fingerprint $current)."

try {
    & $rclone copy $latest.FullName "${remoteName}:NinjaTrader8_Backups/$($latest.Name)" --create-empty-src-dirs
    if ($LASTEXITCODE -ne 0) {
        throw "rclone exited with code $LASTEXITCODE"
    }
    $stamp = "{0}|{1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $current
    Set-Content -LiteralPath $markerPath -Value $stamp
    Write-UploadLog "Upload complete for $($latest.Name)."
    if (Test-Path -LiteralPath $alertMarker) {
        Remove-Item -LiteralPath $alertMarker -Force -ErrorAction SilentlyContinue
    }
    Invoke-RemoteRetention
} catch {
    Write-UploadLog "Upload FAILED for $($latest.Name): $_"
    Invoke-StalenessCheck $latest.Name $latest.FullName
}

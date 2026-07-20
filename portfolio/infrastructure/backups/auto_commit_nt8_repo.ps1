# Auto-commit the top-level NT8 repo (source + BCDR docs only; see .gitignore).
#
# LOCAL COMMIT ONLY -- this repo has no remote by design (.gitignore header), so
# there is nothing to push and this script never attempts one. It is version
# control, not backup; off-site coverage is the MEGA upload elsewhere.
#
# Scoped to the top-level repo ONLY. It deliberately does NOT touch
# bin/Custom/Strategies (the soy repo) -- bin/ is git-excluded here, and strategy
# code must never be auto-committed or pushed.
#
# Run every 5 min by the NT8AutoCommit scheduled task. No-ops when the tree is
# clean, so an idle run costs one `git status` and writes nothing.

$ErrorActionPreference = 'Stop'

$repo = 'C:\Users\<user>\Documents\NinjaTrader 8'
$git  = 'C:\Program Files\Git\cmd\git.exe'
$log  = Join-Path $repo 'Maintenance\auto_commit_nt8_repo.log'

function Write-Log($msg) {
    $line = ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Add-Content -LiteralPath $log -Value $line -Encoding utf8
}

try {
    Set-Location -LiteralPath $repo

    # Anything to commit? --porcelain is empty on a clean tree.
    $dirty = & $git status --porcelain
    if ([string]::IsNullOrWhiteSpace(($dirty -join ''))) {
        exit 0
    }

    & $git add -A

    # Re-check the INDEX: `git add` of ignored-only churn can still stage nothing.
    & $git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        exit 0
    }

    $msg = 'Auto-commit NT8 repo {0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    & $git commit -m $msg | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $head = & $git rev-parse --short HEAD
        Write-Log ('Committed {0}: {1}' -f $head, $msg)
    } else {
        Write-Log ('git commit failed, exit {0}' -f $LASTEXITCODE)
    }
}
catch {
    Write-Log ('ERROR: {0}' -f $_.Exception.Message)
    exit 1
}

# Stages, commits, and pushes any pending changes in the park repo.
# Safe to run repeatedly - no-ops if there's nothing to commit.
# The existing local pre-push hook still scans for PII before anything leaves the machine.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

git add -A

$staged = git diff --cached --name-only
if (-not $staged) {
    Write-Host "Nothing to commit."
    exit 0
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "Autopush: $timestamp"
git push

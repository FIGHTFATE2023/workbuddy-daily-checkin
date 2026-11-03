# WorkBuddy Daily Checkin - uninstaller
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
#         add -Purge to also delete the installed scripts and credentials

param([switch]$Purge)

$taskName = "WorkBuddyDailyCheckin"
$dest = Join-Path $env:USERPROFILE ".workbuddy\wb-checkin"

$t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($t) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Scheduled task '$taskName' removed."
} else {
    Write-Host "Scheduled task '$taskName' not found (nothing to do)."
}

if ($Purge) {
    if (Test-Path $dest) {
        Remove-Item $dest -Recurse -Force -Confirm:$false
        Write-Host "Removed folder: $dest"
    }
} else {
    Write-Host "Kept $dest (scripts, log, credentials). Use -Purge to delete it too."
    Write-Host "NOTE: credentials.json contains your personal token - keep or shred it consciously."
}

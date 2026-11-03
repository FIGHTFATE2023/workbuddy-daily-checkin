# WorkBuddy Daily Checkin - one-click installer
#
# What it does:
#   1. Locate a Python 3.9+ interpreter (no third-party packages needed)
#   2. Copy checkin.py / bridge.py to %USERPROFILE%\.workbuddy\wb-checkin
#   3. Run a smoke test (--status)
#   4. Register a Windows scheduled task "WorkBuddyDailyCheckin"
#      that runs daily at 08:00 / 08:05 / 08:20 (idempotent, safe to repeat)
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
#         (or double-click install.cmd)

$ErrorActionPreference = "Stop"

$src      = $PSScriptRoot
$dest     = Join-Path $env:USERPROFILE ".workbuddy\wb-checkin"
$taskName = "WorkBuddyDailyCheckin"

Write-Host "== WorkBuddy Daily Checkin installer =="

# ---- 1. locate Python -------------------------------------------------------
$pyExe = $null
$pyPrefix = @()
foreach ($cand in @(
    @{ exe = "python";  prefix = @()   },
    @{ exe = "py";      prefix = @("-3") },
    @{ exe = "python3"; prefix = @()   }
)) {
    try {
        & $cand.exe (@($cand.prefix) + @("--version")) *> $null
        if ($LASTEXITCODE -eq 0) { $pyExe = $cand.exe; $pyPrefix = $cand.prefix; break }
    } catch { }
}
if (-not $pyExe) {
    # fall back to the Python bundled with the WorkBuddy desktop app
    $bundled = Get-ChildItem "$env:USERPROFILE\.workbuddy\binaries\python\versions" `
        -Filter python.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($bundled) { $pyExe = $bundled.FullName }
}
if (-not $pyExe) {
    Write-Error "Python 3.9+ not found. Install Python (python.org) and re-run."
    exit 1
}
Write-Host "[1/4] Python found: $pyExe"

# ---- 2. copy scripts --------------------------------------------------------
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $src "checkin.py") $dest -Force
Copy-Item (Join-Path $src "bridge.py")  $dest -Force
$script = Join-Path $dest "checkin.py"
Write-Host "[2/4] Scripts installed to: $dest"

# ---- 3. smoke test ----------------------------------------------------------
Write-Host "[3/4] Smoke test (status check) ..."
& $pyExe (@($pyPrefix) + @($script, "--status", "--json"))
Write-Host ""
Write-Host "      - 'today_checked_in' / ok:true  => the whole chain works."
Write-Host "      - 'no credentials' message      => start the WorkBuddy client first,"
Write-Host "        then run:  python `"$script`" --json"
Write-Host "        (the bridge will fetch a token from the running client)."
Write-Host ""

# ---- 4. register scheduled task ---------------------------------------------
$argParts = @($pyPrefix) + @('"' + $script + '"', "--notify")
$action   = New-ScheduledTaskAction -Execute $pyExe -Argument ($argParts -join " ") -WorkingDirectory $dest
$triggers = @("08:00", "08:05", "08:20") | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Settings $settings -Description "Claim WorkBuddy daily credits (idempotent)." -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host "[4/4] Scheduled task '$taskName' registered: state=$($task.State), next run=$($info.NextRunTime)"
Write-Host ""
Write-Host "Done. Log file: $dest\checkin.log"
Write-Host "Uninstall: powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1"

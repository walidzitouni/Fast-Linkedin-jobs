# Registers a daily 08:30 Task Scheduler task that runs the LinkedIn job scan.
# Run once from an elevated or normal PowerShell window:
#   powershell -ExecutionPolicy Bypass -File install_daily_task.ps1
# To change the time, edit $RunTime below and re-run (the task is overwritten).
# To remove the task:  schtasks /Delete /TN "RapidLinkedInJobs-Daily" /F

$TaskName = "RapidLinkedInJobs-Daily"
$RunTime  = "08:30"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatPath  = Join-Path $ScriptDir "run_daily.bat"

if (-not (Test-Path $BatPath)) {
    Write-Error "run_daily.bat not found in $ScriptDir"
    exit 1
}

schtasks /Create /TN $TaskName /TR "`"$BatPath`"" /SC DAILY /ST $RunTime /F
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Task '$TaskName' registered: runs daily at $RunTime."
    Write-Host "Test it now with:  schtasks /Run /TN `"$TaskName`""
    Write-Host "Reports land in:   $ScriptDir\exports\jobs_latest.xlsx"
    Write-Host "Remove it with:    schtasks /Delete /TN `"$TaskName`" /F"
} else {
    Write-Host ""
    Write-Host "Registration failed (often 'Access denied'). Fix:"
    Write-Host "  1. Close this window."
    Write-Host "  2. Right-click PowerShell -> 'Run as administrator'."
    Write-Host "  3. cd `"$ScriptDir`""
    Write-Host "  4. powershell -ExecutionPolicy Bypass -File install_daily_task.ps1"
    exit $LASTEXITCODE
}

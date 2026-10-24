@echo off
rem Daily scan entry point used by Windows Task Scheduler.
rem Uses the project .venv if present, otherwise the system Python launcher.
cd /d "%~dp0"
if not exist logs mkdir logs
if exist ".venv\Scripts\python.exe" (
    ".\.venv\Scripts\python.exe" scanner.py >> "logs\daily_console.log" 2>&1
) else (
    py -3 scanner.py >> "logs\daily_console.log" 2>&1
)
exit /b %errorlevel%

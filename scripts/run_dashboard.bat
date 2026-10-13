@echo off
rem Swing auto-trader management window (Tkinter). Read-only + buy-halt request. Does not start or stop the daemon/runner.
cd /d "%~dp0\.."
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw -m dashboard.desktop
) else (
  python -m dashboard.desktop
  pause
)

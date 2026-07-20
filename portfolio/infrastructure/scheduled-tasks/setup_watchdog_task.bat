@echo off
REM Setup script for ML Service Watchdog task
REM Run this as Administrator

echo Creating TemaLimitMLServiceWatchdog task...
echo This task will check service health every 1 minute and restart if needed.

schtasks /create ^
  /tn TemaLimitMLServiceWatchdog ^
  /tr "wscript.exe \"C:\Users\<user>\Documents\NinjaTrader 8\MLService\ml_daily_hidden.vbs\"" ^
  /sc minute ^
  /mo 1 ^
  /ru SYSTEM ^
  /rl HIGHEST ^
  /f

if %ERRORLEVEL% EQU 0 (
  echo.
  echo SUCCESS: Watchdog task created!
  echo Task will run every 1 minute starting from next system boot.
  schtasks /query /tn TemaLimitMLServiceWatchdog /v
) else (
  echo.
  echo FAILED: Could not create task. Verify you're running as Administrator.
)

pause

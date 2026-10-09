@echo off
rem Double-click to install everything the app needs (Windows rig PC).
cd /d "%~dp0"
call "%~dp0scripts\find-uv.bat"
if not defined UV goto :nouv
echo Using uv: %UV%
"%UV%" sync --extra nidaq
if errorlevel 1 goto :failed
echo.
echo Setup complete. Double-click start-rig.bat to run on the real rig.
pause
exit /b 0

:nouv
echo.
echo Could not find or install uv. Check the internet connection and try again.
pause
exit /b 1

:failed
echo.
echo Setup FAILED - see the messages above.
pause
exit /b 1

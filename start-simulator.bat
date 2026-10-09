@echo off
rem Double-click to start the targeting window on the SIMULATED rig (no hardware).
cd /d "%~dp0"
call "%~dp0scripts\find-uv.bat"
if not defined UV goto :nouv
"%UV%" run galvo
if errorlevel 1 pause
exit /b 0

:nouv
echo uv not found - double-click setup.bat first.
pause
exit /b 1

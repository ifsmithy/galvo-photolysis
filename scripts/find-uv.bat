@echo off
rem Sets UV to a usable uv.exe. Looks where uv is normally installed (including winget),
rem and if none is found installs a private copy into this project's .tools folder,
rem which needs no PATH changes and no admin rights.
set "UV="
set "ROOT=%~dp0.."
where uv >nul 2>nul && set "UV=uv" && goto :eof
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" & goto :eof
for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv*") do if exist "%%D\uv.exe" set "UV=%%D\uv.exe"
if defined UV goto :eof
if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe" & goto :eof
if exist "%ROOT%\.tools\uv.exe" set "UV=%ROOT%\.tools\uv.exe" & goto :eof
if exist "%ROOT%\.tools\bin\uv.exe" set "UV=%ROOT%\.tools\bin\uv.exe" & goto :eof
echo uv not found: installing a private copy into this folder...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR='%ROOT%\.tools'; $env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex"
if exist "%ROOT%\.tools\uv.exe" set "UV=%ROOT%\.tools\uv.exe"
if exist "%ROOT%\.tools\bin\uv.exe" set "UV=%ROOT%\.tools\bin\uv.exe"

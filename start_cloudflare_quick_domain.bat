@echo off
setlocal
cd /d "%~dp0"
title Image Matcher - Cloudflare Default Domain

echo.
echo ==========================================
echo   Image Matcher - Cloudflare Default Domain
echo ==========================================
echo.

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"

if "%PYTHON_CMD%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if "%PYTHON_CMD%"=="" (
  echo Python was not found on this computer.
  echo Please install Python first:
  echo https://www.python.org/downloads/
  pause
  exit /b 1
)

set "CLOUDFLARED_CMD="
where cloudflared >nul 2>nul
if not errorlevel 1 set "CLOUDFLARED_CMD=cloudflared"
if "%CLOUDFLARED_CMD%"=="" if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CLOUDFLARED_CMD=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if "%CLOUDFLARED_CMD%"=="" if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CLOUDFLARED_CMD=%ProgramFiles%\cloudflared\cloudflared.exe"

if "%CLOUDFLARED_CMD%"=="" (
  echo cloudflared was not found.
  echo.
  echo Download Cloudflare Tunnel client:
  echo https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
  echo.
  echo Put cloudflared.exe in this folder or install it into PATH.
  pause
  exit /b 1
)

if not exist work mkdir work

echo Checking packages...
%PYTHON_CMD% -c "import flask, requests, PIL, imagehash" >nul 2>nul
if errorlevel 1 (
  echo Installing required packages...
  %PYTHON_CMD% -m ensurepip --upgrade > work\install_packages.log 2>&1
  %PYTHON_CMD% -m pip install --user -r requirements.txt >> work\install_packages.log 2>&1
)

%PYTHON_CMD% -c "import flask, requests, PIL, imagehash" >nul 2>nul
if errorlevel 1 (
  echo Package installation failed. Please check:
  echo %cd%\work\install_packages.log
  start notepad "%cd%\work\install_packages.log"
  pause
  exit /b 1
)

echo Checking shared image library...
if not exist "\\Oygx2026\欧野图库区域" (
  echo Cannot access: \\Oygx2026\欧野图库区域
  echo Please run this on a company computer that can open the shared image library.
  pause
  exit /b 1
)

echo Starting local panel...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F >nul 2>nul
)

start "Image Matcher Server" /min cmd /k "%PYTHON_CMD% app.py >> work\panel_server.log 2>&1"
timeout /t 5 /nobreak >nul

echo.
echo Cloudflare will now create a default temporary domain.
echo Copy the https://xxxxx.trycloudflare.com address shown below and send it to the team.
echo.
echo Important: this address may change after restarting this file.
echo.

"%CLOUDFLARED_CMD%" tunnel --url http://127.0.0.1:5000

pause
endlocal

@echo off
setlocal
cd /d "%~dp0"
title Image Matcher Cloudflare Tunnel

echo.
echo ==========================================
echo   Image Matcher - Cloudflare Tunnel Mode
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
  echo Please install Python 3.11 or newer first:
  echo https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

if not exist work mkdir work

echo Checking Python packages...
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

echo Checking shared folders...
if not exist "\\Oygx2026\欧野图库区域" (
  echo Cannot access: \\Oygx2026\欧野图库区域
  echo Please run this on a company computer that can open the shared image library.
  pause
  exit /b 1
)

if not exist "\\Oygx2026\业务-运营共享文件\image_matcher_cloud_index" (
  mkdir "\\Oygx2026\业务-运营共享文件\image_matcher_cloud_index" >nul 2>nul
)

set "CLOUDFLARED_CMD="
where cloudflared >nul 2>nul
if not errorlevel 1 set "CLOUDFLARED_CMD=cloudflared"
if "%CLOUDFLARED_CMD%"=="" if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CLOUDFLARED_CMD=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if "%CLOUDFLARED_CMD%"=="" if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CLOUDFLARED_CMD=%ProgramFiles%\cloudflared\cloudflared.exe"

if "%CLOUDFLARED_CMD%"=="" (
  echo cloudflared was not found.
  echo.
  echo Install method:
  echo 1. Open https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
  echo 2. Download cloudflared for Windows amd64
  echo 3. Put cloudflared.exe in this folder or install it into PATH
  echo.
  pause
  exit /b 1
)

if "%CLOUDFLARE_TUNNEL_TOKEN%"=="" (
  echo CLOUDFLARE_TUNNEL_TOKEN is not set.
  echo.
  echo Please create a Cloudflare Tunnel in Zero Trust, then set the token:
  echo setx CLOUDFLARE_TUNNEL_TOKEN "your-token-here"
  echo.
  echo After setting it, close this window and run this file again.
  pause
  exit /b 1
)

echo Starting local image matcher panel...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F >nul 2>nul
)

start "Image Matcher Server" /min cmd /k "%PYTHON_CMD% app.py >> work\panel_server.log 2>&1"

echo Waiting for local panel...
timeout /t 5 /nobreak >nul

echo Starting Cloudflare Tunnel...
echo Business users should open:
echo https://image-search.1184360066.xyz
echo.
"%CLOUDFLARED_CMD%" tunnel run --token "%CLOUDFLARE_TUNNEL_TOKEN%"

pause
endlocal

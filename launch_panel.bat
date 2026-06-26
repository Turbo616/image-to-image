@echo off
setlocal
cd /d "%~dp0"
title Image Matcher Panel

echo.
echo ==========================================
echo   Image Matcher Panel - Team Launcher
echo ==========================================
echo.

set "PYTHON_CMD="
where python >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python"

if "%PYTHON_CMD%"=="" (
  where py >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if "%PYTHON_CMD%"=="" (
  echo Python was not found on this computer.
  echo.
  echo Trying to install Python automatically with Windows winget...
  where winget >nul 2>nul
  if errorlevel 1 goto python_manual_install

  winget install --id Python.Python.3.12 -e --source winget --accept-source-agreements --accept-package-agreements
  if errorlevel 1 goto python_manual_install

  echo.
  echo Python installer finished. Refreshing command path...
  set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%PATH%"

  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"

  if "%PYTHON_CMD%"=="" (
    where py >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
  )

  if not "%PYTHON_CMD%"=="" goto python_ready

:python_manual_install
  echo.
  echo Automatic Python installation was not available on this computer.
  echo.
  echo Please install Python 3.11 or newer first.
  echo Download page:
  echo https://www.python.org/downloads/
  echo.
  echo Important: during installation, tick "Add python.exe to PATH".
  echo Then double-click open_panel.vbs again.
  echo.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)

:python_ready

if not exist work mkdir work

echo Python detected. Checking required packages...
%PYTHON_CMD% -c "import flask, requests, PIL, imagehash" >nul 2>nul
if not errorlevel 1 goto packages_ok

echo Some packages are missing. Installing packages for current user...
echo Install log: %cd%\work\install_packages.log
echo.

%PYTHON_CMD% -m ensurepip --upgrade > work\install_packages.log 2>&1
if exist vendor\wheels (
  echo Trying local offline packages...
  %PYTHON_CMD% -m pip install --user --no-index --find-links vendor\wheels -r requirements.txt >> work\install_packages.log 2>&1
  if not errorlevel 1 goto packages_verify
)

echo Local offline install was not available or failed. Trying normal internet install...
%PYTHON_CMD% -m pip install --user -r requirements.txt >> work\install_packages.log 2>&1
if not errorlevel 1 goto packages_verify

echo First install attempt failed. Trying Tsinghua mirror...
%PYTHON_CMD% -m pip install --user -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn >> work\install_packages.log 2>&1
if not errorlevel 1 goto packages_verify

echo Second install attempt failed. Trying Aliyun mirror...
%PYTHON_CMD% -m pip install --user -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com >> work\install_packages.log 2>&1
if not errorlevel 1 goto packages_verify

echo.
echo Package installation failed.
echo.
echo Please send this file to the administrator:
echo %cd%\work\install_packages.log
echo.
echo Common fixes:
echo 1. Make sure this computer can access the internet.
echo 2. Make sure Python was installed with "Add python.exe to PATH".
echo 3. If the company network blocks Python packages, ask IT to install these packages:
echo    Pillow, ImageHash, Flask, requests
echo.
start notepad "%cd%\work\install_packages.log"
pause
exit /b 1

:packages_verify
%PYTHON_CMD% -c "import flask, requests, PIL, imagehash" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Packages were installed, but Python still cannot import them.
  echo Please send this file to the administrator:
  echo %cd%\work\install_packages.log
  start notepad "%cd%\work\install_packages.log"
  pause
  exit /b 1
)

:packages_ok
echo Required packages are ready.

echo.
echo Starting panel on this computer...
echo Browser address: http://127.0.0.1:5000/
echo.

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do (
  echo Port 5000 is already in use. Trying to stop old panel process...
  taskkill /PID %%a /F >nul 2>nul
)

echo Launching the server...
start "Image Matcher Server" /min cmd /k "%PYTHON_CMD% app.py >> work\panel_server.log 2>&1"

echo Waiting a few seconds before opening the browser...
timeout /t 5 /nobreak >nul

start "" http://127.0.0.1:5000/

echo Browser opened. If the page is still loading, wait a few seconds and refresh.
echo Keep the minimized "Image Matcher Server" window running while using the panel.
echo If the page later stops, double-click open_panel.vbs again.
echo Log file:
echo %cd%\work\panel_server.log
echo.
pause
endlocal

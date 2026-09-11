@echo off
cd /d "%~dp0"
if not exist work mkdir work
start "Image Matcher Panel" /min python app.py
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:5000

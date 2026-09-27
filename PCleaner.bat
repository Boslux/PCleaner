@echo off
cd /d "%~dp0"
python pcleaner.py %*
if errorlevel 1 pause

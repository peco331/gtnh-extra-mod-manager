@echo off
rem GTNH Extra Mod Manager - CLI version
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
py -m gtnhmod cli
if errorlevel 1 pause

@echo off
rem GTNH Extra Mod Manager - GUI (no console window)
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
where pyw >nul 2>nul || (echo Python pyw launcher not found. Install Python with the py launcher option. & pause & exit /b 1)
start "" pyw -m gtnhmod gui

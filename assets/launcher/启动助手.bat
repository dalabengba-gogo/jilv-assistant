@echo off
rem ================================================================
rem  xyfz_auto portable launcher - double click to start
rem  Starts GUI with bundled runtime (pythonw, no console window)
rem ================================================================
cd /d "%~dp0app\xyfz_auto"
start "" "%~dp0runtime\pythonw.exe" gui_main.py

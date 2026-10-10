@echo off
rem ================================================================
rem  xyfz_auto portable launcher - DEBUG mode (with console window)
rem  Use this when the GUI fails to start, to see error messages.
rem ================================================================
cd /d "%~dp0app\xyfz_auto"
"%~dp0runtime\python.exe" gui_main.py
pause

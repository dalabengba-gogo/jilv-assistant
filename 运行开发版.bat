@echo off
rem ================================================================
rem  从源码直接运行, 开发时用, 不经过打包
rem  用的是 vendor\runtime 里的 Python, 与绿色版运行时完全一致
rem ================================================================
cd /d "%~dp0app\xyfz_auto"
start "" "%~dp0vendor\runtime\pythonw.exe" gui_main.py

@echo off
rem ================================================================
rem  清理残留进程, 删不掉本文件夹时双击这个
rem  只结束从本包目录启动的 python 与 adb, 不动系统 Python 和 MuMu 的 adb
rem ================================================================
cd /d "%~dp0"
"%~dp0runtime\python.exe" "%~dp0app\xyfz_auto\scripts\cleanup_residual.py"
echo.
pause

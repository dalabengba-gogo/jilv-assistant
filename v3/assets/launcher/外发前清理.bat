@echo off
rem ============================================================
rem  外发前清理: 删掉本机个人设置/日志/截图, 再拷给别人
rem  (免责声明标记、定时任务、端口账号表、运行日志)
rem ============================================================
cd /d "%~dp0"
"%~dp0runtime\python.exe" "%~dp0app\xyfz_auto\scripts\cleanup_for_share.py"
echo.
pause

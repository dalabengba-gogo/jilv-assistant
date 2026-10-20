@echo off
rem ================================================================
rem  打包绿色便携版, 输出到 dist 下的 鸡驴助手_v版本号 目录
rem  版本号自动从 app\xyfz_auto\common.py 的 APP_VERSION 读取
rem
rem  可选参数(可叠加):
rem    build.bat --zip      额外压成 zip 包, 约 1~3 分钟
rem    build.bat --clean    写入前先清空同名目标目录
rem ================================================================
cd /d "%~dp0"
"%~dp0vendor\runtime\python.exe" "%~dp0tools\build_portable.py" %*
echo.
pause

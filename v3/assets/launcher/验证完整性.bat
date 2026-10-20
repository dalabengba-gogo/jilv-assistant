@echo off
rem ================================================================
rem  Verify the downloaded zip file (MD5 / SHA256)
rem  Place this .bat next to xxx.zip, then double-click it.
rem  You can also drag a .zip onto this file to verify.
rem ================================================================
"%~dp0runtime\python.exe" "%~dp0app\xyfz_auto\scripts\verify_zip.py" %*
echo.
pause

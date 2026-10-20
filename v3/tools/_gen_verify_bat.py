# -*- coding: utf-8 -*-
import os, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 生成「验证完整性.bat」到源码 assets/launcher/。
# 硬性约定(见 build_portable.py validate 与项目记忆):
#   唯一安全组合 = GBK 编码 + CRLF 换行 + 不写 chcp + 不含硬编码绝对路径。
# 内容纯 ASCII, GBK 解码安全; 路径全部用 %~dp0 相对根目录, 跨电脑可用。
BAT_PATH = r"D:\鸡驴助手_dev\v3\assets\launcher\验证完整性.bat"
os.makedirs(os.path.dirname(BAT_PATH), exist_ok=True)

CONTENT = (
    "@echo off\r\n"
    "rem ================================================================\r\n"
    "rem  Verify the downloaded zip file (MD5 / SHA256)\r\n"
    "rem  Place this .bat next to xxx.zip, then double-click it.\r\n"
    "rem  You can also drag a .zip onto this file to verify.\r\n"
    "rem ================================================================\r\n"
    '"%~dp0runtime\\python.exe" "%~dp0app\\xyfz_auto\\scripts\\verify_zip.py" %*\r\n'
    "echo.\r\n"
    "pause\r\n"
)

with open(BAT_PATH, "wb") as f:
    f.write(CONTENT.encode("gbk"))
print("wrote:", BAT_PATH)

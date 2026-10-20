#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验「鸡驴助手绿色版」压缩包有没有被人改过 / 下载时不完整。

用法(推荐):
    解压后双击里面的「验证完整性.bat」
命令行:
    runtime\\python.exe app\\xyfz_auto\\scripts\\verify_zip.py [压缩包路径]

原理:
    算出压缩包的 MD5 与 SHA256, 再与打包时生成的官方记录
    (鸡驴助手_v4.1.0.md5.txt, 与压缩包放在一起) 逐项比对。
    两个值都一致 = 你拿到的是原封不动的包。

注意:
    这里刻意没有把校验值塞进压缩包内部 —— 塞在里面等于让小偷自己改分数,
    毫无意义。校验值必须是**独立于压缩包**的一份, 最好由发布方走另一个渠道
    (发布页/群公告) 公布, 这样连"包和校验文件一起被换掉"也能防住。
"""

import hashlib
import os
import sys

CHUNK = 1 << 20           # 1MB 分块读, 不至于把 130MB 一次读进内存
ZIP_PREFIX = "鸡驴助手_v"


def _gbk_safe(text):
    """控制台是 GBK 的, 特殊符号(勾/叉/警告)编码不了会直接抛异常。

    历史教训: 绿色版在 cmd 下刷屏 UnicodeEncodeError, 就是这些符号惹的祸。
    所以输出一律只用汉字 + ASCII。
    """
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode(enc, "replace").decode(enc, "replace")


def out(line=""):
    print(_gbk_safe(line))


def find_zip(explicit=None):
    """按 命令行参数 -> 脚本所在包目录 -> 其父目录 -> 当前目录 的顺序找压缩包。"""
    if explicit:
        return explicit if os.path.isfile(explicit) else None

    here = os.path.dirname(os.path.abspath(__file__))
    # 脚本在 <包>/app/xyfz_auto/scripts/ 下, 往上三级才是包根目录
    pkg = os.path.abspath(os.path.join(here, "..", "..", ".."))
    cands = [pkg, os.path.dirname(pkg), os.getcwd()]
    for d in cands:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        hits = sorted(n for n in names
                      if n.startswith(ZIP_PREFIX) and n.lower().endswith(".zip"))
        if hits:
            return os.path.join(d, hits[-1])
    return None


def calc(path):
    """算 MD5 + SHA256 + 字节数, 顺带显示进度(大文件读起来要几秒)。"""
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            size += len(b)
            md5.update(b)
            sha.update(b)
    return md5.hexdigest(), sha.hexdigest(), size


def load_official(zip_path):
    """读官方记录文件: <zip 名>.md5.txt"""
    sidecar = zip_path + ".md5.txt"
    if not os.path.isfile(sidecar):
        # 兼容 鸡驴助手_v4.1.0.md5.txt(不带 .zip) 的写法
        alt = os.path.splitext(zip_path)[0] + ".md5.txt"
        if os.path.isfile(alt):
            sidecar = alt
        else:
            return None, None, None
    md5 = sha = None
    try:
        with open(sidecar, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                low = s.lower()
                if low.startswith("md5"):
                    md5 = s.split(":", 1)[-1].strip().lower()
                elif low.startswith("sha256"):
                    sha = s.split(":", 1)[-1].strip().lower()
    except OSError:
        pass
    return sidecar, md5, sha


def human(n):
    return "%.2f MB (%d 字节)" % (n / 1048576.0, n)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    zip_path = find_zip(args[0] if args else None)

    out("=" * 60)
    out("  鸡驴助手  压缩包完整性校验")
    out("=" * 60)
    out()

    if not zip_path or not os.path.isfile(zip_path):
        out("  没有找到压缩包。")
        out()
        out("  请把本 bat 与 鸡驴助手_vX.Y.Z.zip 放在同一层目录再运行,")
        out("  或者直接把 zip 拖到本 bat 上。")
        out()
        out("  还没解压也能自己验, 在 zip 所在目录打开 PowerShell 执行:")
        out('    (Get-FileHash "鸡驴助手_v4.1.0.zip" -Algorithm MD5).Hash')
        return 2

    out("  文件   : %s" % os.path.basename(zip_path))
    if args:
        out("  位置   : %s" % zip_path)

    sidecar, off_md5, off_sha = load_official(zip_path)

    out()
    out("  正在计算 (100MB 以上约需几秒, 请稍候) ...")
    md5, sha, size = calc(zip_path)

    out()
    out("  大小   : %s" % human(size))
    out("  MD5    : %s" % md5)
    out("  SHA256 : %s" % sha)

    out()
    if not sidecar:
        out("  没有找到官方记录文件 (.md5.txt)。")
        out()
        out("  拿上面这两个值, 跟发布页/群公告里公布的官方 MD5 对比一下,")
        out("  完全一致就说明包是原版的、下载也完整。")
        out()
        out("  提示: 把发布方给的 .md5.txt 放到本文件旁边, 下次就能自动比对。")
        return 0

    out("  官方记录: %s" % os.path.basename(sidecar))
    ok = True
    for label, mine, theirs in (("MD5   ", md5, off_md5), ("SHA256", sha, off_sha)):
        if not theirs:
            continue
        same = (mine == theirs)
        ok = ok and same
        out("  %s : %s  [%s]" % (label, theirs, "一致" if same else "不一致"))

    out()
    if ok:
        out("  结论   : [通过] 与官方记录完全一致。")
        out("           包是原版的, 下载也完整, 可以放心用。")
    else:
        out("  结论   : [警告] 与官方记录不一致!")
        out("           这个包可能被别人改过, 或者下载时坏了。")
        out("           建议: 重新下载, 并且不要运行来路不明的版本。")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

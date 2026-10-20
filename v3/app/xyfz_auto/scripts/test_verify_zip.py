# -*- coding: utf-8 -*-
"""verify_zip.py 的回归测试 —— 别人拿到包后能不能发现问题包。

覆盖四种情况:
    1. 与官方记录一致   → 放行(returncode 0)
    2. MD5 被改过       → 报警(returncode 1)
    3. SHA256 被改过    → 报警(returncode 1)
    4. 没有官方记录文件 → 只报算出的值, 不算失败(returncode 0)

用 1MB 的假包来测: 脚本是通用的, 校验逻辑跟包大小没关系,
没必要为了跑测试真去读 130MB。
"""

import hashlib
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "verify_zip.py")

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % msg)
    else:
        FAIL += 1
        print("  [FAIL] %s" % msg)


def make_fake_zip(path, size=1 << 20):
    """造一个 1MB 的假包(内容随机, 保证两次 md5 不同)。"""
    data = os.urandom(size)
    with open(path, "wb") as f:
        f.write(data)
    return (hashlib.md5(data).hexdigest(),
            hashlib.sha256(data).hexdigest(),
            len(data))


def write_sidecar(zip_path, md5, sha):
    side = os.path.splitext(zip_path)[0] + ".md5.txt"
    with open(side, "w", encoding="utf-8") as f:
        f.write("# 测试用\n")
        f.write("文件: %s\n" % os.path.basename(zip_path))
        f.write("MD5:   %s\n" % md5)
        f.write("SHA256: %s\n" % sha)
    return side


def run(zip_path):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, SCRIPT, zip_path],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=env)
    text = p.stdout.decode("utf-8", "ignore")
    return p.returncode, text


def main():
    tmp = tempfile.mkdtemp(prefix="verify_zip_test_")
    zp = os.path.join(tmp, "鸡驴助手_v9.9.9.zip")
    md5, sha, size = make_fake_zip(zp)

    print("=== 1. 官方记录一致 → 放行")
    side = write_sidecar(zp, md5, sha)
    rc, out = run(zp)
    check("一致" in out, "输出显示 MD5 一致")
    check("一致" in out and out.count("不一致") == 0, "没有报不一致")
    check("通过" in out, "结论是[通过]")
    check(rc == 0, "返回码 0 (实测 %d)" % rc)

    print("=== 2. MD5 被改过 → 报警")
    bad_md5 = "0" * 32
    write_sidecar(zp, bad_md5, sha)
    rc, out = run(zp)
    check("不一致" in out, "输出显示 MD5 不一致")
    check("警告" in out, "结论是[警告]")
    check(rc == 1, "返回码 1 (实测 %d)" % rc)

    print("=== 3. SHA256 被改过 → 也要报警(不能只验 MD5)")
    write_sidecar(zp, md5, "f" * 64)
    rc, out = run(zp)
    check("不一致" in out, "输出显示 SHA256 不一致")
    check(rc == 1, "返回码 1 (实测 %d)" % rc)

    print("=== 4. 没有官方记录 → 只报算出的值, 不算失败")
    os.remove(side)
    rc, out = run(zp)
    check("没有找到官方记录" in out, "提示缺少官方记录")
    check(md5 in out, "仍然把算出的 MD5 显示出来给用户自己比对")
    check(rc == 0, "返回码 0 (缺记录不算失败, 实测 %d)" % rc)

    print("=== 5. 算出的是真的 MD5(拿独立实现核对, 别自说自话)")
    h = hashlib.md5()
    with open(zp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    check(h.hexdigest() == md5, "MD5 与 hashlib 直接算的一致")
    check(size == 1 << 20, "大小统计正确")

    print()
    if FAIL:
        print("*** %d 项未通过 ***" % FAIL)
        return 1
    print("ALL PASS (%d 项)" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())

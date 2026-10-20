# -*- coding: utf-8 -*-
"""桩测试: **文本日志**滚动 (P0-4, 2026-10-18 v4.6.0)

不连模拟器。以前 rotate_files 只管失败截图, run_<port>.log 这种文本日志
从来没有滚动(只靠"跑完任务清一次"), 不跑任务的天只进不出, 实测 logs 撑到
159MB。本脚本验证新增的 rotate_text_logs:

  1. 昨天写的 run_*.log → 按日期归档成 run_x_<YYYYMMDD>.log
  2. 今天写过的 run_*.log → **不归档**(对应的任务可能还在跑, 改名会把文件
     从写方手里抽走)
  3. skip_names 里的文件(正在跑的账号)即使隔夜也不归档
  4. 超过 keep_days 的归档件被删
  5. crash.log **不被归档**(faulthandler 握着句柄, 改名会让崩溃记录继续写进旧文件)
  6. 非 .log 文件完全不动; 目录不存在不报错
  7. 同一天多次归档不互相覆盖(加时间戳后缀)
"""
import os
import sys
import time
import shutil
import tempfile
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())

from common import rotate_text_logs          # noqa: E402

FAILS = []


def check(cond, msg):
    print(f"  {'[OK]' if cond else '[NG]'} {msg}")
    if not cond:
        FAILS.append(msg)


def touch(d, name, age_days=0):
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(b"line\n")
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return p


def names(d):
    return sorted(os.listdir(d))


today_tag = datetime.now().strftime("%Y%m%d")
y_tag = datetime.fromtimestamp(time.time() - 86400).strftime("%Y%m%d")

print("[1] 昨天写的 run_*.log → 按日期归档")
d = tempfile.mkdtemp(prefix="logrot_")
touch(d, "run_16384.log", age_days=1)
res = rotate_text_logs(d, keep_days=7)
check(res["archived"] == 1, f"归档 1 个(实测 {res['archived']})")
check(f"run_16384_{y_tag}.log" in names(d),
      f"归档文件名带昨天日期 {y_tag}(实测 {names(d)})")
check("run_16384.log" not in names(d), "原文件名已腾出, 今天可以重新写")

print("\n[2] 今天写过的 run_*.log → 不归档(可能还在跑)")
d = tempfile.mkdtemp(prefix="logrot_")
touch(d, "run_16384.log", age_days=0)
res = rotate_text_logs(d, keep_days=7)
check(res["archived"] == 0, f"今天写过的不得归档(实测 {res['archived']})")
check(names(d) == ["run_16384.log"], f"文件原封不动(实测 {names(d)})")

print("\n[3] skip_names 里的文件隔夜也不归档(正在跑的账号)")
d = tempfile.mkdtemp(prefix="logrot_")
touch(d, "run_16384.log", age_days=1)
touch(d, "run_16416.log", age_days=1)
res = rotate_text_logs(d, keep_days=7, skip_names={"run_16384.log"})
check(res["archived"] == 1, f"只归档没有被 skip 的那一个(实测 {res['archived']})")
check("run_16384.log" in names(d), "skip 的文件仍在原位(写方没被抽走文件)")
check(f"run_16416_{y_tag}.log" in names(d), "未被 skip 的正常归档")

print("\n[4] 超过 keep_days 的归档件被删")
d = tempfile.mkdtemp(prefix="logrot_")
touch(d, "run_16384_20261001.log", age_days=30)     # 很老的归档件
touch(d, "run_16416.log", age_days=1)               # 昨天 → 该归档不该删
res = rotate_text_logs(d, keep_days=7)
check(res["removed"] == 1, f"删掉 1 个过期归档(实测 {res['removed']})")
check("run_16384_20261001.log" not in names(d), "30 天前的归档件已删")
check(f"run_16416_{y_tag}.log" in names(d), "昨天的只是归档, 没被删")

print("\n[5] crash.log 不归档(faulthandler 常驻句柄)")
d = tempfile.mkdtemp(prefix="logrot_")
touch(d, "crash.log", age_days=1)
res = rotate_text_logs(d, keep_days=7)
check(res["archived"] == 0, f"crash.log 不得改名(实测 {res['archived']})")
check("crash.log" in names(d), "crash.log 原地保留")

print("\n[6] 非 .log 不动 / 目录不存在不报错")
d = tempfile.mkdtemp(prefix="logrot_")
touch(d, "gui_settings.json", age_days=3)
touch(d, "run_16384.log", age_days=1)
res = rotate_text_logs(d, keep_days=7)
check("gui_settings.json" in names(d), "非 .log 文件原样保留")
res2 = rotate_text_logs(os.path.join(d, "不存在的目录/xyz"), keep_days=7)
check(res2 == {"archived": 0, "removed": 0}, f"目录不存在 → 计数全 0(实测 {res2})")

print("\n[7] 同一天多次归档不互相覆盖")
d = tempfile.mkdtemp(prefix="logrot_")
touch(d, "run_16384.log", age_days=1)
rotate_text_logs(d, keep_days=7)                    # 第一轮: 归档成 _<y_tag>
touch(d, "run_16384.log", age_days=1)               # 又冒出一个同名文件(昨天的)
res = rotate_text_logs(d, keep_days=7)
arch = [n for n in names(d) if n.startswith("run_16384_")]
check(len(arch) == 2, f"两份都在, 没被覆盖成一坨(实测 {arch})")
check(len(set(arch)) == 2, "文件名互不相同")

for dd in [d]:
    shutil.rmtree(dd, ignore_errors=True)

print()
if FAILS:
    print(f"✘ 有 {len(FAILS)} 项未通过:")
    for m in FAILS:
        print("   -", m)
    sys.exit(1)
print("✔ 全部通过")
